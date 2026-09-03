"""线程安全的内存适配器，供 CLI、测试和小型独立程序使用。"""

from __future__ import annotations

import base64
import binascii
import threading
from datetime import datetime, timedelta

from ...collection.model import RunRecord
from ...contracts.change import ChangePage, ChangeQuery, DocumentView, SnapshotTimeline
from ...contracts.observation import IngestKey, SubjectRef
from ...history.model import HistoryResult, HistoryWrite
from ...history.ports import HistoryStore


class InMemoryRunStateStore:
    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def find_by_idempotency(self, scope_key: str, idempotency_key: str):
        with self._lock:
            run_id = self._idempotency.get((scope_key, idempotency_key))
            return self._records.get(run_id) if run_id else None

    def create(self, record: RunRecord) -> None:
        with self._lock:
            if record.run_id in self._records:
                raise ValueError(f"重复 run_id: {record.run_id}")
            key = (record.scope_key, record.context.idempotency_key)
            if key in self._idempotency:
                raise ValueError("重复幂等键")
            self._records[record.run_id] = record
            self._idempotency[key] = record.run_id

    def get(self, run_id: str):
        with self._lock:
            return self._records.get(run_id)

    def save(self, record: RunRecord) -> None:
        with self._lock:
            if record.run_id not in self._records:
                raise ValueError(f"未知 run_id: {record.run_id}")
            self._records[record.run_id] = record

    def claim_runnable(self, now: datetime, limit: int, lease_owner: str, lease_seconds: float):
        with self._lock:
            candidates = []
            for record in self._records.values():
                if record.terminal:
                    continue
                if record.next_wakeup_at and record.next_wakeup_at > now:
                    continue
                if record.lease_until and record.lease_until > now and record.lease_owner != lease_owner:
                    continue
                record.lease_owner = lease_owner
                record.lease_until = now + timedelta(seconds=lease_seconds)
                candidates.append(record)
                if len(candidates) >= limit:
                    break
            return tuple(candidates)

    def list_incomplete(self):
        with self._lock:
            return tuple(record for record in self._records.values() if not record.terminal)


class InMemoryHistoryStore(HistoryStore):
    def __init__(self) -> None:
        self._ingest: dict[tuple[str, str, str], tuple[str, HistoryResult]] = {}
        self._documents: dict[tuple[str, str, str, str], object] = {}
        self._snapshots: dict[str, list] = {}
        self._events: dict[str, list] = {}
        self._all_events: list = []
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

    def commit(self, write: HistoryWrite) -> None:
        with self._lock:
            key = (write.result.observation.scope_key, *write.ingest_key)
            existing = self._ingest.get(key)
            if existing is not None:
                if existing[0] != write.observation_fingerprint:
                    raise ValueError("历史幂等键冲突")
                return

            document = write.result.document
            subject_key = _subject_key(document.scope_key, document.subject)
            old_document = self._documents.get(subject_key)
            if old_document is not None and old_document.document_id != document.document_id:
                raise ValueError("同一 subject 生成了不同 document_id")
            snapshots = self._snapshots.get(document.document_id, [])
            snapshot = write.result.snapshot
            if snapshot is not None:
                if any(item.snapshot_id == snapshot.snapshot_id for item in snapshots):
                    raise ValueError("重复 snapshot_id")
                expected_revision = snapshots[-1].revision + 1 if snapshots else 1
                if snapshot.revision != expected_revision:
                    raise ValueError("Snapshot revision 不连续")
            events = self._events.get(document.document_id, [])
            expected_sequence = events[-1].sequence + 1 if events else 1
            for event in write.result.events:
                if event.sequence != expected_sequence:
                    raise ValueError("ChangeEvent sequence 不连续")
                expected_sequence += 1

            # 所有不变量先验证完，再一次性改变内存状态，模拟持久化适配器
            # 的单事务提交语义。
            if snapshot is not None:
                self._snapshots.setdefault(document.document_id, snapshots)
                snapshots.append(snapshot)
            for event in write.result.events:
                self._events.setdefault(document.document_id, events)
                events.append(event)
                self._all_events.append(event)
            self._documents[subject_key] = document
            self._ingest[key] = (write.observation_fingerprint, write.result)

    def get_current(self, scope_key: str, document_id: str):
        with self._lock:
            for document in self._documents.values():
                if document.scope_key == scope_key and document.document_id == document_id:
                    current = None
                    if document.current_snapshot_id:
                        current = next(
                            (item for item in self._snapshots.get(document_id, ()) if item.snapshot_id == document.current_snapshot_id),
                            None,
                        )
                    return DocumentView(document=document, current_snapshot=current)
            return None

    def get_timeline(self, scope_key: str, document_id: str):
        with self._lock:
            view = self.get_current(scope_key, document_id)
            if view is None:
                return None
            return SnapshotTimeline(
                document=view.document,
                snapshots=tuple(self._snapshots.get(document_id, ())),
                events=tuple(self._events.get(document_id, ())),
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


class _StoredIngest:
    def __init__(self, fingerprint, result) -> None:
        self.fingerprint = fingerprint
        self.result = result


def _subject_key(scope_key: str, subject: SubjectRef):
    return scope_key, subject.namespace, subject.key, subject.identity_version


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
