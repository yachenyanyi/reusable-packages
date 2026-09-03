"""关系型 RunStateStore 实现。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ....collection.model import RunRecord, RunStatus
from ....collection.ports import RunStateConflictError
from ....runtime.allocation import (
    allocation_allowed,
    candidate_gateway,
    candidate_sort_key,
    reserve_counts,
    rotate_scopes,
)
from ....runtime.model import AllocationRequest
from .codecs import decode_run, encode_run, idempotency_hash, json_dumps, scope_hash
from .errors import PersistenceInvariantError, PersistenceUnavailableError
from .schema import RelationalTables, db_datetime, read_db_datetime, require_sqlalchemy
from .transaction import write_transaction


class RelationalRunStateStore:
    """将完整 RunRecord 作为适配器私有载荷保存，并维护领取索引。"""

    def __init__(self, engine: Any, tables: RelationalTables, dialect: Any) -> None:
        self._engine = engine
        self._tables = tables
        self._dialect = dialect

    def find_by_idempotency(self, scope_key: str, idempotency_key: str):
        sa = require_sqlalchemy()
        table = self._tables.runs
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.select(table).where(
                        table.c.scope_hash == scope_hash(scope_key),
                        table.c.idempotency_hash == idempotency_hash(scope_key, idempotency_key),
                    )
                ).mappings().first()
            return self._decode_row(row, scope_key=scope_key, idempotency_key=idempotency_key) if row else None
        except PersistenceInvariantError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法读取 Run 幂等记录") from exc

    def create(self, record: RunRecord) -> None:
        sa = require_sqlalchemy()
        table = self._tables.runs
        _validate_run_scope(record)
        values = _row_values(record)
        try:
            with self._engine.begin() as connection:
                connection.execute(table.insert().values(**values))
        except sa.exc.IntegrityError as exc:
            # 核心会重新查询幂等记录，以存储中的 Run 为准。
            raise ValueError("重复 run_id 或幂等键") from exc
        except Exception as exc:
            raise PersistenceUnavailableError("无法创建 Run") from exc

    def get(self, scope_key: str, run_id: str):
        sa = require_sqlalchemy()
        table = self._tables.runs
        try:
            with self._engine.connect() as connection:
                row = connection.execute(
                    sa.select(table).where(
                        table.c.scope_hash == scope_hash(scope_key),
                        table.c.run_id == run_id,
                    )
                ).mappings().first()
            return self._decode_row(row, scope_key=scope_key) if row else None
        except PersistenceInvariantError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法读取 Run") from exc

    def save(self, record: RunRecord) -> None:
        sa = require_sqlalchemy()
        table = self._tables.runs
        _validate_run_scope(record)
        expected_version = record.state_version
        next_version = expected_version + 1
        values = _row_values(record)
        values.pop("run_id")
        values["state_version"] = next_version
        try:
            with self._engine.begin() as connection:
                result = connection.execute(
                    sa.update(table)
                    .where(
                        table.c.run_id == record.run_id,
                        table.c.scope_hash == scope_hash(record.scope_key),
                        table.c.state_version == expected_version,
                    )
                    .values(**values)
                )
                if result.rowcount == 0:
                    current = connection.execute(
                        sa.select(table.c.state_version).where(
                            table.c.run_id == record.run_id,
                            table.c.scope_hash == scope_hash(record.scope_key),
                        )
                    ).scalar()
                    if current is None:
                        raise ValueError(f"未知 run_id: {record.run_id}")
                    raise RunStateConflictError("Run 状态发生并发冲突，当前副本已过期")
            record.state_version = next_version
        except RunStateConflictError:
            raise
        except ValueError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法保存 Run") from exc

    def allocate(self, request: AllocationRequest):
        if not isinstance(request, AllocationRequest):
            raise ValueError("allocate 需要 AllocationRequest")
        sa = require_sqlalchemy()
        table = self._tables.runs
        terminal = tuple(status.value for status in (
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ))
        now = request.now
        lease_until = now + timedelta(seconds=request.lease_seconds)
        now_value = db_datetime(now)
        try:
            with write_transaction(self._engine, self._dialect) as connection:
                state_statement = sa.select(self._tables.allocator_state).where(
                    self._tables.allocator_state.c.component == "work_allocator"
                )
                state_row = connection.execute(
                    self._dialect.lock_state(state_statement)
                ).mappings().first()
                if state_row is None:
                    raise PersistenceUnavailableError("工作分配器状态未初始化")
                last_scope_hash = state_row["last_scope_hash"]

                active_where = sa.and_(
                    ~table.c.status.in_(terminal),
                    table.c.lease_until.is_not(None),
                    table.c.lease_until > now_value,
                )
                active_total = connection.execute(
                    sa.select(sa.func.count()).select_from(table).where(active_where)
                ).scalar_one()
                active_scope = {
                    row[0]: row[1]
                    for row in connection.execute(
                        sa.select(table.c.scope_key, sa.func.count())
                        .where(active_where)
                        .group_by(table.c.scope_hash, table.c.scope_key)
                    ).all()
                }
                active_gateway = {
                    row[0]: row[1]
                    for row in connection.execute(
                        sa.select(table.c.gateway_key, sa.func.count())
                        .where(active_where, table.c.gateway_key.is_not(None))
                        .group_by(table.c.gateway_key)
                    ).all()
                }

                eligible_where = sa.and_(
                    ~table.c.status.in_(terminal),
                    sa.or_(table.c.next_wakeup_at.is_(None), table.c.next_wakeup_at <= now_value),
                    sa.or_(
                        table.c.lease_until.is_(None),
                        table.c.lease_until <= now_value,
                        table.c.lease_owner == request.worker_id,
                    ),
                )
                scope_rows = connection.execute(
                    sa.select(table.c.scope_hash)
                    .where(eligible_where)
                    .distinct()
                ).all()
                scopes = rotate_scopes(
                    sorted({row[0] for row in scope_rows}),
                    last_scope_hash,
                )
                claimed = []
                claimed_ids: set[str] = set()
                while len(claimed) < request.batch_size:
                    selected_in_round = False
                    for scope_hash_value in scopes:
                        candidate_rows = connection.execute(
                            self._dialect.lock_rows(
                                sa.select(table)
                                .where(
                                    eligible_where,
                                    table.c.scope_hash == scope_hash_value,
                                    *(
                                        [~table.c.run_id.in_(claimed_ids)]
                                        if claimed_ids
                                        else []
                                    ),
                                )
                                .order_by(table.c.accepted_at, table.c.run_id)
                            )
                        ).mappings().all()
                        decoded_candidates = sorted(
                            ((row, self._decode_row(row)) for row in candidate_rows),
                            key=lambda item: candidate_sort_key(item[1]),
                        )
                        for row, record in decoded_candidates:
                            if row["run_id"] in claimed_ids:
                                continue
                            if not allocation_allowed(
                                record,
                                request,
                                active_scope,
                                active_gateway,
                                active_total,
                            ):
                                continue
                            current_version = record.state_version
                            was_active_same_owner = (
                                record.lease_until is not None
                                and record.lease_until > request.now
                                and record.lease_owner == request.worker_id
                            )
                            reserve_counts(record, request, active_scope, active_gateway)
                            result = connection.execute(
                                sa.update(table)
                                .where(
                                    table.c.run_id == record.run_id,
                                    table.c.scope_hash == scope_hash_value,
                                    table.c.state_version == current_version,
                                )
                                .values(
                                    lease_owner=request.worker_id,
                                    lease_until=db_datetime(lease_until),
                                    state_version=current_version + 1,
                                )
                            )
                            if result.rowcount != 1:
                                raise RunStateConflictError("Run 领取状态发生并发冲突")
                            record.lease_owner = request.worker_id
                            record.lease_until = lease_until
                            record.state_version = current_version + 1
                            claimed.append(record)
                            claimed_ids.add(record.run_id)
                            if not was_active_same_owner:
                                active_total += 1
                            last_scope_hash = scope_hash_value
                            selected_in_round = True
                            break
                        if len(claimed) >= request.batch_size:
                            break
                    if not selected_in_round:
                        break
                if claimed:
                    connection.execute(
                        sa.update(self._tables.allocator_state)
                        .where(self._tables.allocator_state.c.component == "work_allocator")
                        .values(
                            last_scope_hash=last_scope_hash,
                            updated_at=db_datetime(now),
                        )
                    )
            return tuple(claimed)
        except (PersistenceInvariantError, RunStateConflictError):
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法领取可运行 Run") from exc

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
        sa = require_sqlalchemy()
        table = self._tables.runs
        terminal = tuple(status.value for status in (
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_ERRORS,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ))
        try:
            with self._engine.connect() as connection:
                rows = connection.execute(
                    sa.select(table)
                    .where(~table.c.status.in_(terminal))
                    .order_by(table.c.accepted_at, table.c.run_id)
                ).mappings().all()
            return tuple(self._decode_row(row) for row in rows)
        except PersistenceInvariantError:
            raise
        except Exception as exc:
            raise PersistenceUnavailableError("无法列出未完成 Run") from exc

    def _decode_row(self, row, *, scope_key: str | None = None, idempotency_key: str | None = None):
        if row is None:
            return None
        record = decode_run(row["record_json"])
        if record.run_id != row["run_id"] or record.scope_key != row["scope_key"]:
            raise PersistenceInvariantError("Run 持久化载荷与索引不一致")
        if record.scope_key != record.context.scope_key:
            raise PersistenceInvariantError("Run scope 与 ExecutionContext 不一致")
        if (
            row["scope_hash"] != scope_hash(record.scope_key)
            or row["idempotency_hash"]
            != idempotency_hash(record.scope_key, record.context.idempotency_key)
            or row["request_fingerprint"] != record.request_fingerprint
            or row["gateway_key"] != candidate_gateway(record)
        ):
            raise PersistenceInvariantError("Run 索引投影与载荷不一致")
        if scope_key is not None and record.scope_key != scope_key:
            raise PersistenceInvariantError("Run 幂等索引 scope 不一致")
        if idempotency_key is not None and record.context.idempotency_key != idempotency_key:
            raise PersistenceInvariantError("Run 幂等索引内容不一致")
        # 元数据列是领取索引的投影；以它恢复租约，防止领取后返回旧载荷。
        record.lease_owner = row["lease_owner"]
        record.lease_until = read_db_datetime(row["lease_until"])
        record.next_wakeup_at = read_db_datetime(row["next_wakeup_at"])
        state_version = row["state_version"]
        if isinstance(state_version, bool) or not isinstance(state_version, int) or state_version < 0:
            raise PersistenceInvariantError("Run 状态版本无效")
        record.state_version = state_version
        if record.status.value != row["status"]:
            raise PersistenceInvariantError("Run 状态投影与载荷不一致")
        return record


def _row_values(record: RunRecord) -> dict[str, Any]:
    _validate_run_scope(record)
    return {
        "run_id": record.run_id,
        "scope_hash": scope_hash(record.scope_key),
        "scope_key": record.scope_key,
        "idempotency_hash": idempotency_hash(record.scope_key, record.context.idempotency_key),
        "idempotency_key": record.context.idempotency_key,
        "request_fingerprint": record.request_fingerprint,
        "status": record.status.value,
        "gateway_key": candidate_gateway(record),
        "accepted_at": db_datetime(record.accepted_at),
        "updated_at": db_datetime(record.updated_at),
        "next_wakeup_at": db_datetime(record.next_wakeup_at) if record.next_wakeup_at else None,
        "lease_owner": record.lease_owner,
        "lease_until": db_datetime(record.lease_until) if record.lease_until else None,
        "state_version": record.state_version,
        "record_json": json_dumps(encode_run(record)),
    }


def _validate_run_scope(record: RunRecord) -> None:
    if record.scope_key != record.context.scope_key:
        raise ValueError("Run 的 scope_key 必须与 ExecutionContext 一致")
