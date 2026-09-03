"""内容历史深模块。"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import replace
from typing import Any

from ..contracts.change import (
    ChangeEvent,
    ChangeKind,
    Document,
    DocumentState,
    Snapshot,
)
from ..contracts.observation import Observation, Presence
from ..contracts.primitives import new_id, stable_json, utc_now
from ..errors import HistoryInvariantError, IdempotencyConflictError
from ..runtime.registry import ExtensionRegistry
from .model import ComparisonContext, HistoryResult, HistoryWrite
from .ports import EventSink, HistoryStore


class ContentHistory:
    """把 Observation 变成不可变快照和变化事件。

    调用方不能直接创建 Snapshot 或 ChangeEvent；身份、规范化、哈希和缺失
    判断全部由已注册的 ContentPolicy 与本模块共同维护。
    """

    def __init__(
        self,
        store: HistoryStore,
        registry: ExtensionRegistry,
        *,
        event_sink: EventSink | None = None,
        clock: Any | None = None,
    ) -> None:
        self._store = store
        self._registry = registry
        self._event_sink = event_sink
        self._clock = clock or _SystemClock()
        self._lock = threading.RLock()

    def record(self, observation: Observation) -> HistoryResult:
        with self._lock:
            return self._record(observation)

    def _record(self, observation: Observation) -> HistoryResult:
        stored = self._store.get_by_ingest_key(observation.scope_key, observation.ingest_key)
        fingerprint = _observation_fingerprint(observation)
        if stored is not None:
            if stored.fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    "同一 ingest_key 对应了不同的 Observation，拒绝覆盖历史"
                )
            return replace(stored.result, duplicate=True)

        if observation.presence is Presence.PRESENT:
            assert observation.content is not None
            policy = self._registry.content_policy(
                observation.content.type_key,
                observation.content.schema_version,
            )
        else:
            policy = self._registry.content_policy_by_namespace(observation.subject.namespace)

        if policy.subject_namespace != observation.subject.namespace:
            raise HistoryInvariantError("Observation.subject.namespace 与 ContentPolicy 不匹配")

        current_view = None
        existing_document = self._store.get_document(observation.scope_key, observation.subject)
        if existing_document is not None and existing_document.policy_ref != policy.policy_ref:
            raise HistoryInvariantError(
                "同一 Document 不能静默切换 ContentPolicy；请先执行显式迁移"
            )
        if existing_document is not None:
            current_view = self._store.get_current(
                observation.scope_key,
                existing_document.document_id,
            )
        previous_snapshot = current_view.current_snapshot if current_view else None

        material = None
        if observation.presence is Presence.PRESENT:
            material = policy.prepare_revision(observation.content)

        comparison_context = ComparisonContext(
            document=existing_document,
            previous_snapshot=previous_snapshot,
            consecutive_absences=existing_document.missing_streak if existing_document else 0,
        )
        decision = policy.compare(
            previous_snapshot,
            observation,
            material,
            comparison_context,
        )
        if decision is None:
            raise HistoryInvariantError("ContentPolicy 必须返回 RevisionDecision")
        if existing_document is None and observation.presence is Presence.ABSENT and decision.kind not in {
            ChangeKind.MISSING_SUSPECTED,
            ChangeKind.MISSING_CONFIRMED,
        }:
            raise HistoryInvariantError("首次 ABSENT Observation 必须产生缺失判断")
        if existing_document is not None:
            if (
                existing_document.state is DocumentState.PRESENT
                and observation.presence is Presence.ABSENT
                and decision.kind is None
            ):
                raise HistoryInvariantError("PRESENT Document 的有效缺失观察不能被静默忽略")
            if (
                existing_document.state is DocumentState.MISSING_CONFIRMED
                and observation.presence is Presence.PRESENT
                and decision.kind is not ChangeKind.RESTORED
            ):
                raise HistoryInvariantError("确认缺失后的首次有效出现必须产生 RESTORED")
            if (
                existing_document.state is DocumentState.MISSING_CONFIRMED
                and observation.presence is Presence.ABSENT
                and decision.kind is ChangeKind.MISSING_SUSPECTED
            ):
                raise HistoryInvariantError("已确认缺失的 Document 不能退回怀疑状态")

        document_id = (
            existing_document.document_id
            if existing_document is not None
            else _document_id(observation.scope_key, observation.subject)
        )
        now = self._clock.now()
        snapshot = None
        current_snapshot = previous_snapshot

        if observation.presence is Presence.PRESENT and decision.create_snapshot:
            if material is None:
                raise HistoryInvariantError("PRESENT Observation 缺少 RevisionMaterial")
            next_revision = (previous_snapshot.revision + 1) if previous_snapshot else 1
            snapshot = Snapshot(
                snapshot_id=new_id(),
                document_id=document_id,
                scope_key=observation.scope_key,
                revision=next_revision,
                observed_at=observation.observed_at,
                recorded_at=now,
                content=material.normalized_content,
                content_hash=material.content_hash,
                run_id=observation.run_id,
                observation_id=observation.observation_id,
                provenance=observation.provenance,
            )
            current_snapshot = snapshot
        elif observation.presence is Presence.ABSENT and decision.create_snapshot:
            raise HistoryInvariantError("ABSENT Observation 不能创建 Snapshot")

        if existing_document is None:
            document_state = _initial_document_state(observation, decision.kind)
            missing_streak = 1 if observation.presence is Presence.ABSENT else 0
            first_observed_at = observation.observed_at
        else:
            document_state = _next_document_state(existing_document, observation, decision.kind)
            missing_streak = (
                existing_document.missing_streak + 1
                if observation.presence is Presence.ABSENT
                else 0
            )
            first_observed_at = existing_document.first_observed_at

        document = Document(
            document_id=document_id,
            scope_key=observation.scope_key,
            subject=observation.subject,
            state=document_state,
            current_snapshot_id=current_snapshot.snapshot_id if current_snapshot else None,
            first_observed_at=first_observed_at,
            last_observed_at=max(existing_document.last_observed_at, observation.observed_at)
            if existing_document
            else observation.observed_at,
            missing_streak=missing_streak,
            policy_ref=policy.policy_ref,
        )

        event = _build_event(
            observation=observation,
            document=document,
            previous_state=existing_document.state if existing_document else None,
            previous_snapshot=previous_snapshot,
            current_snapshot=current_snapshot if observation.presence is Presence.PRESENT else None,
            decision=decision,
            now=now,
            next_sequence=self._next_sequence(observation.scope_key, document_id),
        )
        events = (event,) if event is not None else ()
        result = HistoryResult(
            observation=observation,
            document=document,
            snapshot=snapshot,
            events=events,
        )
        self._store.commit(
            HistoryWrite(
                ingest_key=(observation.ingest_key.gateway_key, observation.ingest_key.upstream_record_id),
                observation_fingerprint=fingerprint,
                result=result,
            )
        )
        if events and self._event_sink is not None:
            # 事实已经在 HistoryStore 中提交。投递失败不能让上层重做历史写入；
            # 具备可靠投递需求时由 EventSink 自身实现 outbox/重投。
            try:
                self._event_sink.publish(events)
            except Exception:
                pass
        return result

    def get_current(self, document_id: str, scope_key: str):
        return self._store.get_current(scope_key, document_id)

    def get_timeline(self, document_id: str, scope_key: str):
        return self._store.get_timeline(scope_key, document_id)

    def query_changes(self, query, scope_key: str):
        return self._store.query_changes(scope_key, query)

    def _next_sequence(self, scope_key: str, document_id: str) -> int:
        timeline = self._store.get_timeline(scope_key, document_id)
        return (timeline.events[-1].sequence + 1) if timeline and timeline.events else 1


class _SystemClock:
    def now(self):
        return utc_now()


def _document_id(scope_key: str, subject) -> str:
    material = stable_json(
        {
            "scope_key": scope_key,
            "namespace": subject.namespace,
            "key": subject.key,
            "identity_version": subject.identity_version,
        }
    ).encode("utf-8")
    return "doc_" + hashlib.sha256(material).hexdigest()[:32]


def _observation_fingerprint(observation: Observation) -> str:
    value = observation.to_dict()
    value.pop("observation_id", None)
    value.pop("received_at", None)
    value.pop("run_id", None)
    provenance = value.get("provenance")
    if isinstance(provenance, dict):
        # 上游任务、Attempt 和采集器引用是传输轨迹，不是记录身份；同一
        # upstream record 在重试或不同 Run 重放时仍应命中同一个 ingest_key。
        for transient_field in ("upstream_job_ref", "attempt_ref", "collector_ref"):
            provenance.pop(transient_field, None)
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _initial_document_state(observation: Observation, kind: ChangeKind | None) -> DocumentState:
    if observation.presence is Presence.PRESENT:
        if kind is not ChangeKind.FIRST_SEEN:
            raise HistoryInvariantError("新建 PRESENT Document 必须产生 FIRST_SEEN")
        return DocumentState.PRESENT
    if kind is ChangeKind.MISSING_CONFIRMED:
        return DocumentState.MISSING_CONFIRMED
    return DocumentState.MISSING_SUSPECTED


def _next_document_state(
    document: Document,
    observation: Observation,
    kind: ChangeKind | None,
) -> DocumentState:
    if observation.presence is Presence.PRESENT:
        return DocumentState.PRESENT
    if kind is ChangeKind.MISSING_CONFIRMED:
        return DocumentState.MISSING_CONFIRMED
    if kind is ChangeKind.MISSING_SUSPECTED:
        return DocumentState.MISSING_SUSPECTED
    return document.state


def _build_event(
    *,
    observation: Observation,
    document: Document,
    previous_state: DocumentState | None,
    previous_snapshot: Snapshot | None,
    current_snapshot: Snapshot | None,
    decision,
    now,
    next_sequence: int,
) -> ChangeEvent | None:
    kind = decision.kind
    if kind is None:
        return None
    if kind is ChangeKind.FIRST_SEEN and current_snapshot is None:
        raise HistoryInvariantError("FIRST_SEEN 必须产生 Snapshot")
    if kind is ChangeKind.REVISED and (
        previous_snapshot is None or current_snapshot is None or previous_snapshot.snapshot_id == current_snapshot.snapshot_id
    ):
        raise HistoryInvariantError("REVISED 必须引用不同的前后 Snapshot")
    if kind is ChangeKind.RESTORED and previous_state is not DocumentState.MISSING_CONFIRMED:
        raise HistoryInvariantError("RESTORED 只能出现在此前已确认缺失的 Document")
    evidence = (
        [observation.provenance.raw_artifact_ref]
        if observation.provenance.raw_artifact_ref
        else [f"observation:{observation.observation_id}"]
    )
    return ChangeEvent(
        event_id=new_id(),
        scope_key=observation.scope_key,
        document_id=document.document_id,
        run_id=observation.run_id,
        sequence=next_sequence,
        kind=kind,
        occurred_at=now,
        effective_observed_at=observation.observed_at,
        from_snapshot_id=previous_snapshot.snapshot_id if previous_snapshot else None,
        to_snapshot_id=current_snapshot.snapshot_id if current_snapshot else None,
        evidence_refs=tuple(evidence),
        policy_ref=document.policy_ref,
        details=decision.details,
    )
