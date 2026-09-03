"""线程安全的内存适配器，供 CLI、测试和小型独立程序使用。"""

from __future__ import annotations

import base64
import binascii
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from ...collection.model import RunRecord
from ...collection.ports import RunStateConflictError
from ...contracts.change import ChangeEvent, ChangePage, ChangeQuery, DocumentView, SnapshotTimeline
from ...contracts.observation import IngestKey, SubjectRef
from ...history.model import HistoryCommitOutcome, HistoryResult, HistoryWrite
from ...history.ports import HistoryStore, HistoryWriteConflictError
from ...runtime.model import (
    AllocationRequest,
    DeliveryClaim,
    DeliveryFailure,
    DeliveryGuarantee,
    DeliveryStatus,
    DispatchRequest,
)
from ...runtime.allocation import (
    allocation_allowed,
    candidate_gateway,
    candidate_sort_key,
    reserve_counts,
    rotate_scopes,
)
from ...runtime.ports import DeliveryLeaseLostError


class InMemoryRunStateStore:
    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()
        self._last_scope_key: str | None = None

    def find_by_idempotency(self, scope_key: str, idempotency_key: str):
        with self._lock:
            run_id = self._idempotency.get((scope_key, idempotency_key))
            record = self._records.get(run_id) if run_id else None
            return _clone_run(record) if record is not None else None

    def create(self, record: RunRecord) -> None:
        with self._lock:
            _validate_run_scope(record)
            if record.run_id in self._records:
                raise ValueError(f"重复 run_id: {record.run_id}")
            key = (record.scope_key, record.context.idempotency_key)
            if key in self._idempotency:
                raise ValueError("重复幂等键")
            record.state_version = 0
            self._records[record.run_id] = _clone_run(record)
            self._idempotency[key] = record.run_id

    def get(self, scope_key: str, run_id: str):
        with self._lock:
            record = self._records.get(run_id)
            if record is None or record.scope_key != scope_key:
                return None
            return _clone_run(record)

    def save(self, record: RunRecord) -> None:
        with self._lock:
            _validate_run_scope(record)
            if record.run_id not in self._records:
                raise ValueError(f"未知 run_id: {record.run_id}")
            current = self._records[record.run_id]
            if current.scope_key != record.scope_key or current.state_version != record.state_version:
                raise RunStateConflictError("Run 状态发生并发冲突，当前副本已过期")
            record.state_version += 1
            self._records[record.run_id] = _clone_run(record)

    def allocate(self, request: AllocationRequest):
        if not isinstance(request, AllocationRequest):
            raise ValueError("allocate 需要 AllocationRequest")
        with self._lock:
            now = request.now
            active = [
                record
                for record in self._records.values()
                if not record.terminal and record.lease_until is not None and record.lease_until > now
            ]
            active_scope = _count_by_scope(active)
            active_gateway = _count_by_gateway(active)

            candidates_by_scope: dict[str, list[RunRecord]] = {}
            for record in tuple(self._records.values()):
                if record.terminal:
                    continue
                if record.next_wakeup_at and record.next_wakeup_at > now:
                    continue
                if record.lease_until and record.lease_until > now and record.lease_owner != request.worker_id:
                    continue
                candidates_by_scope.setdefault(record.scope_key, []).append(record)

            for candidates in candidates_by_scope.values():
                candidates.sort(key=candidate_sort_key)
            scopes = rotate_scopes(sorted(candidates_by_scope), self._last_scope_key)
            candidates = []
            while len(candidates) < request.batch_size:
                selected_in_round = False
                for scope_key in scopes:
                    for record in candidates_by_scope[scope_key]:
                        if any(item.run_id == record.run_id for item in candidates):
                            continue
                        if not allocation_allowed(
                            record,
                            request,
                            active_scope,
                            active_gateway,
                            len(active) + len(candidates),
                        ):
                            continue
                        claimed = _clone_run(record)
                        claimed.lease_owner = request.worker_id
                        claimed.lease_until = now + timedelta(seconds=request.lease_seconds)
                        claimed.state_version += 1
                        self._records[claimed.run_id] = _clone_run(claimed)
                        candidates.append(claimed)
                        reserve_counts(record, request, active_scope, active_gateway)
                        self._last_scope_key = scope_key
                        selected_in_round = True
                        break
                    if len(candidates) >= request.batch_size:
                        break
                if not selected_in_round:
                    break
            return tuple(candidates)

    def claim_runnable(self, now: datetime, limit: int, lease_owner: str, lease_seconds: float):
        """兼容 v0.2 的内部入口；新的运行时统一调用 allocate。"""

        return self.allocate(
            AllocationRequest(
                worker_id=lease_owner,
                now=now,
                batch_size=limit,
                lease_seconds=lease_seconds,
            )
        )

    def list_incomplete(self):
        with self._lock:
            return tuple(_clone_run(record) for record in self._records.values() if not record.terminal)


def _validate_run_scope(record: RunRecord) -> None:
    if record.scope_key != record.context.scope_key:
        raise ValueError("Run 的 scope_key 必须与 ExecutionContext 一致")


def _count_by_scope(records: list[RunRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.scope_key] = counts.get(record.scope_key, 0) + 1
    return counts


def _count_by_gateway(records: list[RunRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        gateway_key = candidate_gateway(record)
        if gateway_key is not None:
            counts[gateway_key] = counts.get(gateway_key, 0) + 1
    return counts




class InMemoryHistoryStore(HistoryStore):
    def __init__(self, *, delivery_guarantee: DeliveryGuarantee = DeliveryGuarantee.NONE) -> None:
        if not isinstance(delivery_guarantee, DeliveryGuarantee):
            raise ValueError("delivery_guarantee 必须是 DeliveryGuarantee")
        self.delivery_guarantee = delivery_guarantee
        self._ingest: dict[tuple[str, str, str], tuple[str, HistoryResult]] = {}
        self._documents: dict[tuple[str, str, str, str], object] = {}
        self._snapshots: dict[tuple[str, str], list] = {}
        self._events: dict[tuple[str, str], list] = {}
        self._all_events: list = []
        self._outbox: dict[str, _MemoryOutboxRecord] = {}
        self._outbox_order: list[str] = []
        self._lock = threading.RLock()

    def get_by_ingest_key(self, scope_key: str, ingest_key: IngestKey):
        with self._lock:
            value = self._ingest.get(
                (scope_key, ingest_key.gateway_key, ingest_key.upstream_record_id)
            )
            return _StoredIngest(*value) if value is not None else None

    def get_document(self, scope_key: str, subject: SubjectRef):
        with self._lock:
            return self._documents.get(_subject_key(scope_key, subject))

    def commit(self, write: HistoryWrite) -> HistoryCommitOutcome:
        with self._lock:
            if not isinstance(write, HistoryWrite):
                raise ValueError("commit 需要 HistoryWrite")
            key = (write.result.observation.scope_key, *write.ingest_key)
            existing = self._ingest.get(key)
            if existing is not None:
                if existing[0] != write.observation_fingerprint:
                    raise ValueError("历史幂等键冲突")
                return HistoryCommitOutcome.DUPLICATE

            document = write.result.document
            subject_key = _subject_key(document.scope_key, document.subject)
            old_document = self._documents.get(subject_key)
            if old_document is not None and old_document.document_id != document.document_id:
                raise ValueError("同一 subject 生成了不同 document_id")
            if old_document != write.base_document:
                raise HistoryWriteConflictError("HistoryWrite 基于的 Document 已被其它写入更新")
            history_key = _history_key(document.scope_key, document.document_id)
            snapshots = self._snapshots.get(history_key, [])
            snapshot = write.result.snapshot
            if snapshot is not None:
                if any(item.snapshot_id == snapshot.snapshot_id for item in snapshots):
                    raise ValueError("重复 snapshot_id")
                expected_revision = snapshots[-1].revision + 1 if snapshots else 1
                if snapshot.revision != expected_revision:
                    raise ValueError("Snapshot revision 不连续")
            events = self._events.get(history_key, [])
            expected_sequence = events[-1].sequence + 1 if events else 1
            for event in write.result.events:
                if event.sequence != expected_sequence:
                    raise ValueError("ChangeEvent sequence 不连续")
                expected_sequence += 1

            # 所有不变量先验证完，再一次性改变内存状态，模拟持久化适配器
            # 的单事务提交语义。
            if snapshot is not None:
                self._snapshots.setdefault(history_key, snapshots)
                snapshots.append(snapshot)
            for event in write.result.events:
                self._events.setdefault(history_key, events)
                events.append(event)
                self._all_events.append(event)
                if self.delivery_guarantee is DeliveryGuarantee.AT_LEAST_ONCE:
                    if event.event_id in self._outbox:
                        raise ValueError("重复 event_id")
                    self._outbox[event.event_id] = _MemoryOutboxRecord(
                        event=event,
                        created_at=event.occurred_at,
                        updated_at=event.occurred_at,
                    )
                    self._outbox_order.append(event.event_id)
            self._documents[subject_key] = document
            self._ingest[key] = (write.observation_fingerprint, write.result)
            return HistoryCommitOutcome.CREATED

    def get_current(self, scope_key: str, document_id: str):
        with self._lock:
            for document in self._documents.values():
                if document.scope_key == scope_key and document.document_id == document_id:
                    current = None
                    if document.current_snapshot_id:
                        current = next(
                            (
                                item
                                for item in self._snapshots.get(_history_key(scope_key, document_id), ())
                                if item.snapshot_id == document.current_snapshot_id
                            ),
                            None,
                        )
                        if current is not None and (
                            current.scope_key != scope_key or current.document_id != document_id
                        ):
                            raise ValueError("Snapshot scope 或 document_id 不一致")
                    return DocumentView(document=document, current_snapshot=current)
            return None

    def get_timeline(self, scope_key: str, document_id: str):
        with self._lock:
            view = self.get_current(scope_key, document_id)
            if view is None:
                return None
            return SnapshotTimeline(
                document=view.document,
                snapshots=tuple(self._snapshots.get(_history_key(scope_key, document_id), ())),
                events=tuple(self._events.get(_history_key(scope_key, document_id), ())),
            )

    def query_changes(self, scope_key: str, query: ChangeQuery) -> ChangePage:
        with self._lock:
            start = _decode_cursor(query.cursor)
            matches = []
            index = start
            while index < len(self._all_events) and len(matches) < query.limit:
                event = self._all_events[index]
                index += 1
                if event.scope_key != scope_key:
                    continue
                if query.document_id and event.document_id != query.document_id:
                    continue
                if query.kinds and event.kind not in query.kinds:
                    continue
                if query.occurred_after and event.occurred_at <= query.occurred_after:
                    continue
                if query.occurred_before and event.occurred_at >= query.occurred_before:
                    continue
                matches.append(event)
            has_more = _has_matching_event(self._all_events, index, scope_key, query)
            next_cursor = _encode_cursor(index) if has_more else None
            return ChangePage(events=tuple(matches), next_cursor=next_cursor, has_more=has_more)

    def claim_pending(self, request: DispatchRequest) -> tuple[DeliveryClaim, ...]:
        if not isinstance(request, DispatchRequest):
            raise ValueError("claim_pending 需要 DispatchRequest")
        with self._lock:
            claims: list[DeliveryClaim] = []
            for event_id in self._outbox_order:
                record = self._outbox[event_id]
                if record.status in {DeliveryStatus.DELIVERED, DeliveryStatus.BLOCKED}:
                    continue
                if record.next_attempt_at > request.now:
                    continue
                if record.status is DeliveryStatus.DELIVERING and record.lease_until > request.now:
                    continue
                if _has_pending_predecessor(self._outbox, record):
                    continue
                record.status = DeliveryStatus.DELIVERING
                record.attempt_count += 1
                record.lease_owner = request.worker_id
                record.lease_until = request.now + timedelta(seconds=request.lease_seconds)
                record.state_version += 1
                record.updated_at = request.now
                claims.append(
                    DeliveryClaim(
                        event=record.event,
                        attempt_count=record.attempt_count,
                        lease_owner=request.worker_id,
                        lease_version=record.state_version,
                    )
                )
                if len(claims) >= request.batch_size:
                    break
            return tuple(claims)

    def mark_delivered(self, claim: DeliveryClaim, now: datetime) -> None:
        with self._lock:
            record = self._require_claim(claim)
            record.status = DeliveryStatus.DELIVERED
            record.lease_owner = None
            record.lease_until = None
            record.delivered_at = now
            record.updated_at = now
            record.state_version += 1

    def reschedule(
        self,
        claim: DeliveryClaim,
        failure: DeliveryFailure,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        with self._lock:
            record = self._require_claim(claim)
            record.status = DeliveryStatus.PENDING
            record.lease_owner = None
            record.lease_until = None
            record.next_attempt_at = next_attempt_at
            record.last_error = failure
            record.updated_at = now
            record.state_version += 1

    def block(self, claim: DeliveryClaim, failure: DeliveryFailure, now: datetime) -> None:
        with self._lock:
            record = self._require_claim(claim)
            record.status = DeliveryStatus.BLOCKED
            record.lease_owner = None
            record.lease_until = None
            record.last_error = failure
            record.updated_at = now
            record.state_version += 1

    def _require_claim(self, claim: DeliveryClaim) -> "_MemoryOutboxRecord":
        if not isinstance(claim, DeliveryClaim):
            raise ValueError("需要 DeliveryClaim")
        record = self._outbox.get(claim.event.event_id)
        if record is None or (
            record.status is not DeliveryStatus.DELIVERING
            or record.lease_owner != claim.lease_owner
            or record.state_version != claim.lease_version
            or record.event != claim.event
        ):
            raise DeliveryLeaseLostError("事件投递租约已失效")
        return record


@dataclass
class _MemoryOutboxRecord:
    event: ChangeEvent
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempt_count: int = 0
    next_attempt_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    lease_owner: str | None = None
    lease_until: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    state_version: int = 0
    last_error: DeliveryFailure | None = None
    created_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))
    delivered_at: datetime | None = None


def _has_pending_predecessor(
    records: dict[str, _MemoryOutboxRecord],
    current: _MemoryOutboxRecord,
) -> bool:
    return any(
        record.event.scope_key == current.event.scope_key
        and record.event.document_id == current.event.document_id
        and record.event.sequence < current.event.sequence
        and record.status is not DeliveryStatus.DELIVERED
        for record in records.values()
    )


class _StoredIngest:
    def __init__(self, fingerprint, result) -> None:
        self.fingerprint = fingerprint
        self.result = result


def _clone_run(record: RunRecord) -> RunRecord:
    """复制 Run 的可变进度字段，保留契约对象的不可变载荷。"""

    return replace(
        record,
        attempts=list(record.attempts),
        processed_ingest_keys=set(record.processed_ingest_keys),
        record_failures=list(record.record_failures),
    )


def _subject_key(scope_key: str, subject: SubjectRef):
    return scope_key, subject.namespace, subject.key, subject.identity_version


def _history_key(scope_key: str, document_id: str) -> tuple[str, str]:
    return scope_key, document_id


def _encode_cursor(index: int) -> str:
    return base64.urlsafe_b64encode(str(index).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        value = int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("无效的变化查询游标") from exc
    if value < 0:
        raise ValueError("无效的变化查询游标")
    return value


def _has_matching_event(events, start: int, scope_key: str, query: ChangeQuery) -> bool:
    return any(
        event.scope_key == scope_key
        and (not query.document_id or event.document_id == query.document_id)
        and (not query.kinds or event.kind in query.kinds)
        and (not query.occurred_after or event.occurred_at > query.occurred_after)
        and (not query.occurred_before or event.occurred_at < query.occurred_before)
        for event in events[start:]
    )
