"""关系型 HistoryStore 实现。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ....contracts.change import ChangeEvent, ChangePage, ChangeQuery, Document, DocumentView, SnapshotTimeline
from ....contracts.observation import IngestKey, SubjectRef
from ....history.model import HistoryCommitOutcome, HistoryResult, HistoryWrite
from ....history.ports import HistoryWriteConflictError
from ....runtime.model import (
    DeliveryClaim,
    DeliveryFailure,
    DeliveryGuarantee,
    DeliveryStatus,
    DispatchRequest,
)
from ....runtime.ports import DeliveryLeaseLostError
from .codecs import (
    decode_document,
    decode_event,
    decode_history_result,
    decode_snapshot,
    encode_history_result,
    ingest_hash,
    json_dumps,
    scope_hash,
    subject_hash,
)
from .errors import PersistenceInvariantError, PersistenceUnavailableError
from .schema import RelationalTables, db_datetime, require_sqlalchemy
from .transaction import write_transaction


class RelationalHistoryStore:
    """在单个关系型事务中提交 Document、Snapshot、ChangeEvent 与幂等结果。"""

    def __init__(
        self,
        engine: Any,
        tables: RelationalTables,
        dialect: Any,
        *,
        delivery_guarantee: DeliveryGuarantee = DeliveryGuarantee.NONE,
    ) -> None:
        self._engine = engine
        self._tables = tables
        self._dialect = dialect
        if not isinstance(delivery_guarantee, DeliveryGuarantee):
            raise ValueError("delivery_guarantee 必须是 DeliveryGuarantee")
        self.delivery_guarantee = delivery_guarantee

    def get_by_ingest_key(self, scope_key: str, ingest_key: IngestKey):
        sa = require_sqlalchemy()
        table = self._tables.history_ingest
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.select(table).where(
                        table.c.scope_hash == scope_hash(scope_key),
                        table.c.ingest_hash == ingest_hash(
                            scope_key,
                            ingest_key.gateway_key,
                            ingest_key.upstream_record_id,
                        )
                    )
                ).mappings().first()
            if row is None:
                return None
            self._verify_ingest_row(row, scope_key, ingest_key)
            return _StoredIngest(row["observation_fingerprint"], decode_history_result(row["result_json"]))
        except PersistenceInvariantError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法读取历史幂等记录") from exc

    def get_document(self, scope_key: str, subject: SubjectRef):
        sa = require_sqlalchemy()
        row = self._find_document(sa, scope_key, subject)
        return _decode_document_row(row, scope_key, subject) if row else None

    def commit(self, write: HistoryWrite) -> HistoryCommitOutcome:
        if not isinstance(write, HistoryWrite):
            raise ValueError("commit 需要 HistoryWrite")
        result = write.result
        observation = result.observation
        scope_key = observation.scope_key
        gateway_key, upstream_record_id = write.ingest_key
        if (gateway_key, upstream_record_id) != (
            observation.ingest_key.gateway_key,
            observation.ingest_key.upstream_record_id,
        ):
            raise PersistenceInvariantError("HistoryWrite 的 ingest_key 与 Observation 不一致")
        sa = require_sqlalchemy()
        ingest_table = self._tables.history_ingest
        try:
            with write_transaction(self._engine, self._dialect) as connection:
                ingest_values = {
                    "ingest_hash": ingest_hash(scope_key, gateway_key, upstream_record_id),
                    "scope_hash": scope_hash(scope_key),
                    "scope_key": scope_key,
                    "gateway_key": gateway_key,
                    "upstream_record_id": upstream_record_id,
                    "observation_fingerprint": write.observation_fingerprint,
                    "result_json": json_dumps(encode_history_result(result)),
                }
                try:
                    with connection.begin_nested():
                        connection.execute(ingest_table.insert().values(**ingest_values))
                except sa.exc.IntegrityError:
                    existing = connection.execute(
                        sa.select(ingest_table).where(
                            ingest_table.c.ingest_hash == ingest_values["ingest_hash"]
                        )
                    ).mappings().first()
                    if existing is None:
                        raise PersistenceUnavailableError("历史幂等记录发生未知冲突")
                    self._verify_ingest_row(existing, scope_key, IngestKey(gateway_key, upstream_record_id))
                    if existing["observation_fingerprint"] != write.observation_fingerprint:
                        raise ValueError("历史幂等键冲突")
                    return HistoryCommitOutcome.DUPLICATE

                documents = self._tables.documents
                document = _select_document(
                    connection,
                    sa,
                    documents,
                    scope_key,
                    result.document.subject,
                    self._dialect,
                )
                if document is not None:
                    existing_document = _decode_document_row(document, scope_key, result.document.subject)
                    if existing_document.document_id != result.document.document_id:
                        raise PersistenceInvariantError("同一 subject 生成了不同 document_id")
                else:
                    existing_document = None

                if existing_document != write.base_document:
                    raise HistoryWriteConflictError(
                        "HistoryWrite 基于的 Document 已被其它写入更新"
                    )

                self._validate_history_rows(connection, sa, result, existing_document)

                if result.snapshot is not None:
                    snapshot = result.snapshot
                    connection.execute(
                        self._tables.snapshots.insert().values(
                            snapshot_id=snapshot.snapshot_id,
                            document_id=snapshot.document_id,
                            scope_hash=scope_hash(snapshot.scope_key),
                            scope_key=snapshot.scope_key,
                            revision=snapshot.revision,
                            observed_at=db_datetime(snapshot.observed_at),
                            recorded_at=db_datetime(snapshot.recorded_at),
                            snapshot_json=json_dumps(snapshot.to_dict()),
                        )
                    )

                for event in result.events:
                    connection.execute(
                        self._tables.events.insert().values(
                            event_id=event.event_id,
                            document_id=event.document_id,
                            scope_hash=scope_hash(event.scope_key),
                            scope_key=event.scope_key,
                            sequence=event.sequence,
                            kind=event.kind.value,
                            occurred_at=db_datetime(event.occurred_at),
                            event_json=json_dumps(event.to_dict()),
                        )
                    )

                if self.delivery_guarantee is DeliveryGuarantee.AT_LEAST_ONCE:
                    for event in result.events:
                        connection.execute(
                            self._tables.outbox.insert().values(
                                event_id=event.event_id,
                                scope_hash=scope_hash(event.scope_key),
                                scope_key=event.scope_key,
                                document_id=event.document_id,
                                sequence=event.sequence,
                                status=DeliveryStatus.PENDING.value,
                                attempt_count=0,
                                next_attempt_at=db_datetime(event.occurred_at),
                                lease_owner=None,
                                lease_until=None,
                                state_version=0,
                                event_json=json_dumps(event.to_dict()),
                                last_error_code=None,
                                last_error_message=None,
                                created_at=db_datetime(event.occurred_at),
                                updated_at=db_datetime(event.occurred_at),
                                delivered_at=None,
                            )
                        )

                document_values = {
                    "document_id": result.document.document_id,
                    "scope_hash": scope_hash(result.document.scope_key),
                    "scope_key": result.document.scope_key,
                    "subject_hash": subject_hash(result.document.subject),
                    "subject_namespace": result.document.subject.namespace,
                    "subject_key": result.document.subject.key,
                    "identity_version": result.document.subject.identity_version,
                    "document_json": json_dumps(result.document.to_dict()),
                }
                if existing_document is None:
                    try:
                        with connection.begin_nested():
                            connection.execute(documents.insert().values(**document_values))
                    except sa.exc.IntegrityError as exc:
                        # 两个 Worker 可能都在事务外基于“没有 Document”完成计算。
                        # 唯一键竞争本身不是持久化损坏，应转换为上层可有限重算的冲突。
                        competing = _select_document(
                            connection,
                            sa,
                            documents,
                            scope_key,
                            result.document.subject,
                            self._dialect,
                        )
                        if competing is not None:
                            _decode_document_row(competing, scope_key, result.document.subject)
                            raise HistoryWriteConflictError(
                                "同一 subject 的 Document 已被其它写入创建"
                            ) from exc
                        raise
                else:
                    connection.execute(
                        sa.update(documents)
                        .where(documents.c.document_id == result.document.document_id)
                        .values(**{key: value for key, value in document_values.items() if key != "document_id"})
                    )
            return HistoryCommitOutcome.CREATED
        except (
            HistoryWriteConflictError,
            PersistenceInvariantError,
            ValueError,
            PersistenceUnavailableError,
        ):
            raise
        except sa.exc.IntegrityError as exc:
            raise PersistenceInvariantError("历史写入违反唯一性或版本连续性约束") from exc
        except Exception as exc:
            if _is_transient_concurrency_error(exc):
                # MySQL 的间隙锁/唯一键竞争可能把两个“首次建档”事务判定为
                # deadlock。事务已经回滚，这不是持久化损坏；交给 History
                # 深模块按有限次数重新读取并计算。
                raise HistoryWriteConflictError("历史写入遇到暂时并发冲突") from exc
            raise PersistenceUnavailableError("无法提交内容历史") from exc

    def get_current(self, scope_key: str, document_id: str) -> DocumentView | None:
        sa = require_sqlalchemy()
        documents = self._tables.documents
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.select(documents).where(
                        documents.c.scope_hash == scope_hash(scope_key),
                        documents.c.document_id == document_id,
                    )
                ).mappings().first()
                if row is None:
                    return None
                document = _decode_indexed_document_row(row, scope_key)
                snapshot = (
                    self._snapshot_for(connection, sa, document.current_snapshot_id, scope_key, document_id)
                    if document.current_snapshot_id
                    else None
                )
                return DocumentView(document, snapshot)
        except PersistenceInvariantError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法读取当前内容历史") from exc

    def get_timeline(self, scope_key: str, document_id: str) -> SnapshotTimeline | None:
        sa = require_sqlalchemy()
        documents = self._tables.documents
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.select(documents).where(
                        documents.c.scope_hash == scope_hash(scope_key),
                        documents.c.document_id == document_id,
                    )
                ).mappings().first()
                if row is None:
                    return None
                document = _decode_indexed_document_row(row, scope_key)
                snapshots = connection.execute(
                    sa.select(self._tables.snapshots)
                    .where(
                        self._tables.snapshots.c.scope_hash == scope_hash(scope_key),
                        self._tables.snapshots.c.document_id == document_id,
                    )
                    .order_by(self._tables.snapshots.c.revision)
                ).mappings().all()
                events = connection.execute(
                    sa.select(self._tables.events)
                    .where(
                        self._tables.events.c.scope_hash == scope_hash(scope_key),
                        self._tables.events.c.document_id == document_id,
                    )
                    .order_by(self._tables.events.c.sequence)
                ).mappings().all()
                return SnapshotTimeline(
                    document=document,
                    snapshots=tuple(
                        _decode_indexed_snapshot_row(row, scope_key, document_id)
                        for row in snapshots
                    ),
                    events=tuple(
                        _decode_indexed_event_row(row, scope_key, document_id)
                        for row in events
                    ),
                )
        except PersistenceInvariantError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法读取内容时间线") from exc

    def query_changes(self, scope_key: str, query: ChangeQuery) -> ChangePage:
        sa = require_sqlalchemy()
        start = _decode_cursor(query.cursor)
        events = self._tables.events
        conditions = [
            events.c.scope_hash == scope_hash(scope_key),
            events.c.event_order > start,
        ]
        if query.document_id:
            conditions.append(events.c.document_id == query.document_id)
        if query.kinds:
            conditions.append(events.c.kind.in_(kind.value for kind in query.kinds))
        if query.occurred_after:
            conditions.append(events.c.occurred_at > db_datetime(query.occurred_after))
        if query.occurred_before:
            conditions.append(events.c.occurred_at < db_datetime(query.occurred_before))
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    sa.select(events)
                    .where(*conditions)
                    .order_by(events.c.event_order)
                    .limit(query.limit + 1)
                ).mappings().all()
            has_more = len(rows) > query.limit
            page_rows = rows[: query.limit]
            page = tuple(
                _decode_indexed_event_row(row, scope_key, row["document_id"])
                for row in page_rows
            )
            next_cursor = (
                _encode_cursor(page_rows[-1]["event_order"])
                if has_more
                else None
            )
            return ChangePage(page, next_cursor, has_more)
        except PersistenceInvariantError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法查询变化事件") from exc

    def claim_pending(self, request: DispatchRequest) -> tuple[DeliveryClaim, ...]:
        if not isinstance(request, DispatchRequest):
            raise ValueError("claim_pending 需要 DispatchRequest")
        sa = require_sqlalchemy()
        table = self._tables.outbox
        predecessor = table.alias("outbox_predecessor")
        now = db_datetime(request.now)
        predecessor_exists = sa.exists(
            sa.select(1)
            .select_from(predecessor)
            .where(
                predecessor.c.document_id == table.c.document_id,
                predecessor.c.scope_hash == table.c.scope_hash,
                predecessor.c.sequence < table.c.sequence,
                predecessor.c.status != DeliveryStatus.DELIVERED.value,
            )
        )
        eligible = sa.and_(
            sa.or_(
                table.c.status == DeliveryStatus.PENDING.value,
                sa.and_(
                    table.c.status == DeliveryStatus.DELIVERING.value,
                    table.c.lease_until.is_not(None),
                    table.c.lease_until <= now,
                ),
            ),
            table.c.next_attempt_at <= now,
            ~predecessor_exists,
        )
        try:
            with write_transaction(self._engine, self._dialect) as connection:
                rows = connection.execute(
                    self._dialect.lock_rows(
                        sa.select(table)
                        .where(eligible)
                        .order_by(table.c.created_at, table.c.event_id)
                        .limit(request.batch_size)
                    )
                ).mappings().all()
                claims: list[DeliveryClaim] = []
                for row in rows:
                    event = _decode_delivery_event(row)
                    state_version = row["state_version"]
                    attempt_count = row["attempt_count"]
                    if (
                        isinstance(state_version, bool)
                        or not isinstance(state_version, int)
                        or state_version < 0
                        or isinstance(attempt_count, bool)
                        or not isinstance(attempt_count, int)
                        or attempt_count < 0
                    ):
                        raise PersistenceInvariantError("事件投递版本或尝试次数无效")
                    next_attempt_count = attempt_count + 1
                    lease_until = request.now + timedelta(seconds=request.lease_seconds)
                    result = connection.execute(
                        sa.update(table)
                        .where(
                            table.c.event_id == event.event_id,
                            table.c.state_version == state_version,
                        )
                        .values(
                            status=DeliveryStatus.DELIVERING.value,
                            attempt_count=next_attempt_count,
                            lease_owner=request.worker_id,
                            lease_until=db_datetime(lease_until),
                            state_version=state_version + 1,
                            updated_at=db_datetime(request.now),
                        )
                    )
                    if result.rowcount != 1:
                        raise DeliveryLeaseLostError("事件投递租约发生并发冲突")
                    claims.append(
                        DeliveryClaim(
                            event=event,
                            attempt_count=next_attempt_count,
                            lease_owner=request.worker_id,
                            lease_version=state_version + 1,
                        )
                    )
            return tuple(claims)
        except (DeliveryLeaseLostError, PersistenceInvariantError):
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法领取待投递事件") from exc

    def mark_delivered(self, claim: DeliveryClaim, now: datetime) -> None:
        self._update_delivery(
            claim,
            {
                "status": DeliveryStatus.DELIVERED.value,
                "lease_owner": None,
                "lease_until": None,
                "delivered_at": db_datetime(now),
                "updated_at": db_datetime(now),
                "state_version": claim.lease_version + 1,
            },
        )

    def reschedule(
        self,
        claim: DeliveryClaim,
        failure: DeliveryFailure,
        next_attempt_at: datetime,
        now: datetime,
    ) -> None:
        self._update_delivery(
            claim,
            {
                "status": DeliveryStatus.PENDING.value,
                "lease_owner": None,
                "lease_until": None,
                "next_attempt_at": db_datetime(next_attempt_at),
                "last_error_code": failure.code,
                "last_error_message": failure.message,
                "updated_at": db_datetime(now),
                "state_version": claim.lease_version + 1,
            },
        )

    def block(self, claim: DeliveryClaim, failure: DeliveryFailure, now: datetime) -> None:
        self._update_delivery(
            claim,
            {
                "status": DeliveryStatus.BLOCKED.value,
                "lease_owner": None,
                "lease_until": None,
                "last_error_code": failure.code,
                "last_error_message": failure.message,
                "updated_at": db_datetime(now),
                "state_version": claim.lease_version + 1,
            },
        )

    def _update_delivery(self, claim: DeliveryClaim, values: dict[str, Any]) -> None:
        if not isinstance(claim, DeliveryClaim):
            raise ValueError("需要 DeliveryClaim")
        sa = require_sqlalchemy()
        table = self._tables.outbox
        try:
            with write_transaction(self._engine, self._dialect) as connection:
                result = connection.execute(
                    sa.update(table)
                    .where(
                        table.c.event_id == claim.event.event_id,
                        table.c.status == DeliveryStatus.DELIVERING.value,
                        table.c.lease_owner == claim.lease_owner,
                        table.c.state_version == claim.lease_version,
                    )
                    .values(**values)
                )
                if result.rowcount != 1:
                    raise DeliveryLeaseLostError("事件投递租约已失效")
        except DeliveryLeaseLostError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法更新事件投递状态") from exc

    def _find_document(self, sa, scope_key: str, subject: SubjectRef):
        try:
            with self._engine.connect() as connection:
                return connection.execute(
                    sa.select(self._tables.documents).where(
                        self._tables.documents.c.scope_hash == scope_hash(scope_key),
                        self._tables.documents.c.subject_hash == subject_hash(subject),
                    )
                ).mappings().first()
        except Exception as exc:
            raise PersistenceUnavailableError("无法读取 Document") from exc

    def _snapshot_for(self, connection, sa, snapshot_id: str, scope_key: str, document_id: str):
        row = connection.execute(
            sa.select(self._tables.snapshots).where(
                self._tables.snapshots.c.scope_hash == scope_hash(scope_key),
                self._tables.snapshots.c.document_id == document_id,
                self._tables.snapshots.c.snapshot_id == snapshot_id,
            )
        ).mappings().first()
        if row is None:
            return None
        return _decode_indexed_snapshot_row(row, scope_key, document_id)

    def _validate_history_rows(
        self,
        connection,
        sa,
        result: HistoryResult,
        existing_document: Document | None,
    ) -> None:
        snapshot = result.snapshot
        if snapshot is not None:
            if snapshot.document_id != result.document.document_id or snapshot.scope_key != result.document.scope_key:
                raise PersistenceInvariantError("Snapshot 不属于当前 Document")
            last = connection.execute(
                sa.select(self._tables.snapshots.c.revision)
                .where(
                    self._tables.snapshots.c.scope_hash == scope_hash(snapshot.scope_key),
                    self._tables.snapshots.c.document_id == snapshot.document_id,
                )
                .order_by(self._tables.snapshots.c.revision.desc())
                .limit(1)
            ).scalar()
            expected = (last + 1) if last is not None else 1
            if snapshot.revision != expected:
                raise PersistenceInvariantError("Snapshot revision 不连续")
        last_sequence = connection.execute(
            sa.select(self._tables.events.c.sequence)
            .where(
                self._tables.events.c.scope_hash == scope_hash(result.document.scope_key),
                self._tables.events.c.document_id == result.document.document_id,
            )
            .order_by(self._tables.events.c.sequence.desc())
            .limit(1)
        ).scalar()
        expected_sequence = (last_sequence + 1) if last_sequence is not None else 1
        for event in result.events:
            if event.document_id != result.document.document_id or event.scope_key != result.document.scope_key:
                raise PersistenceInvariantError("ChangeEvent 不属于当前 Document")
            if event.sequence != expected_sequence:
                raise PersistenceInvariantError("ChangeEvent sequence 不连续")
            expected_sequence += 1
        if existing_document is not None and existing_document.policy_ref != result.document.policy_ref:
            raise PersistenceInvariantError("同一 Document 不能静默切换 ContentPolicy")

    def _verify_ingest_row(self, row, scope_key: str, ingest_key: IngestKey) -> None:
        if (
            row["scope_hash"] != scope_hash(scope_key)
            or row["scope_key"] != scope_key
            or row["ingest_hash"] != ingest_hash(
                scope_key,
                ingest_key.gateway_key,
                ingest_key.upstream_record_id,
            )
            or row["gateway_key"] != ingest_key.gateway_key
            or row["upstream_record_id"] != ingest_key.upstream_record_id
        ):
            raise PersistenceInvariantError("历史幂等索引与载荷不一致")


class _StoredIngest:
    def __init__(self, fingerprint: str, result: HistoryResult) -> None:
        self.fingerprint = fingerprint
        self.result = result


def _select_document(connection, sa, table, scope_key: str, subject: SubjectRef, dialect: Any):
    statement = sa.select(table).where(
        table.c.scope_hash == scope_hash(scope_key),
        table.c.subject_hash == subject_hash(subject),
    )
    return connection.execute(dialect.lock_document(statement)).mappings().first()


def _decode_document_row(row, scope_key: str, subject: SubjectRef) -> Document:
    document = decode_document(row["document_json"])
    if (
        row["document_id"] != document.document_id
        or row["scope_hash"] != scope_hash(scope_key)
        or row["scope_key"] != scope_key
        or row["subject_hash"] != subject_hash(subject)
        or row["subject_namespace"] != subject.namespace
        or row["subject_key"] != subject.key
        or row["identity_version"] != subject.identity_version
        or document.scope_key != scope_key
        or document.subject != subject
    ):
        raise PersistenceInvariantError("Document 索引与载荷不一致")
    return document


def _decode_indexed_document_row(row, scope_key: str) -> Document:
    try:
        subject = SubjectRef(
            row["subject_namespace"],
            row["subject_key"],
            row["identity_version"],
        )
    except (TypeError, ValueError) as exc:
        raise PersistenceInvariantError("Document 身份索引无效") from exc
    return _decode_document_row(row, scope_key, subject)


def _decode_indexed_snapshot_row(row, scope_key: str, document_id: str):
    snapshot = decode_snapshot(row["snapshot_json"])
    if (
        row["snapshot_id"] != snapshot.snapshot_id
        or row["scope_hash"] != scope_hash(scope_key)
        or row["scope_key"] != scope_key
        or row["document_id"] != document_id
        or snapshot.scope_key != scope_key
        or snapshot.document_id != document_id
        or row["revision"] != snapshot.revision
    ):
        raise PersistenceInvariantError("Snapshot 索引与载荷不一致")
    return snapshot


def _decode_indexed_event_row(row, scope_key: str, document_id: str) -> ChangeEvent:
    event = decode_event(row["event_json"])
    if (
        row["event_id"] != event.event_id
        or row["scope_hash"] != scope_hash(scope_key)
        or row["scope_key"] != scope_key
        or row["document_id"] != document_id
        or row["sequence"] != event.sequence
        or row.get("kind") is not None
        and row["kind"] != event.kind.value
        or event.scope_key != scope_key
        or event.document_id != document_id
    ):
        raise PersistenceInvariantError("ChangeEvent 索引与载荷不一致")
    return event


def _decode_delivery_event(row) -> ChangeEvent:
    event = _decode_indexed_event_row(row, row["scope_key"], row["document_id"])
    try:
        status = DeliveryStatus(row["status"])
    except (TypeError, ValueError) as exc:
        raise PersistenceInvariantError("事件投递状态无效") from exc
    if (
        status is DeliveryStatus.DELIVERED
        and row["delivered_at"] is None
    ):
        raise PersistenceInvariantError("Outbox 索引与事件载荷不一致")
    return event


def _encode_cursor(index: int) -> str:
    import base64

    return base64.urlsafe_b64encode(str(index).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    import base64
    import binascii

    try:
        value = int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii"))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise ValueError("无效的变化查询游标") from exc
    if value < 0:
        raise ValueError("无效的变化查询游标")
    return value


def _is_transient_concurrency_error(exc: BaseException) -> bool:
    """识别适配器可以安全交给 History 重算的数据库瞬时冲突。"""

    pending = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))

        values = [getattr(current, "code", None)]
        values.extend(getattr(current, "args", ()) or ())
        original = getattr(current, "orig", None)
        if original is not None:
            values.append(getattr(original, "code", None))
            values.extend(getattr(original, "args", ()) or ())
            pending.append(original)
        if any(
            isinstance(value, (int, str)) and value in {1205, 1213, "1205", "1213"}
            for value in values
        ):
            return True

        message = str(current).lower()
        if "deadlock found" in message or "lock wait timeout exceeded" in message:
            return True
        for related in (current.__cause__, current.__context__):
            if related is not None:
                pending.append(related)
    return False
