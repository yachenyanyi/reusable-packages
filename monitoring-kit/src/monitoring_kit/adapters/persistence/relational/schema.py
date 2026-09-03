"""关系型适配器的私有 schema 与初始化协议。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .errors import PersistenceConfigurationError, PersistenceUnavailableError

SCHEMA_NAME = "monitoring-kit"
SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class RelationalTables:
    metadata: Any
    schema_version: Any
    runs: Any
    history_ingest: Any
    documents: Any
    snapshots: Any
    events: Any
    outbox: Any
    allocator_state: Any


def require_sqlalchemy():
    try:
        import sqlalchemy as sa
    except ModuleNotFoundError as exc:  # pragma: no cover - 由安装组合决定
        raise PersistenceConfigurationError(
            "关系型持久化适配器需要安装 SQLAlchemy；请安装 monitoring-kit[relational]"
        ) from exc
    return sa


def build_tables() -> RelationalTables:
    sa = require_sqlalchemy()
    from sqlalchemy.dialects.mysql import LONGTEXT

    payload_type = sa.Text().with_variant(LONGTEXT(), "mysql")
    mysql_table_options = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    metadata = sa.MetaData()
    schema_version = sa.Table(
        "monitoring_kit_schema_version",
        metadata,
        sa.Column("component", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=False), nullable=False),
        **mysql_table_options,
    )
    runs = sa.Table(
        "monitoring_kit_runs",
        metadata,
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.Text, nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("gateway_key", sa.String(255)),
        sa.Column("accepted_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("next_wakeup_at", sa.DateTime(timezone=False)),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_until", sa.DateTime(timezone=False)),
        sa.Column("state_version", sa.Integer, nullable=False),
        sa.Column("record_json", payload_type, nullable=False),
        sa.UniqueConstraint(
            "scope_hash",
            "idempotency_hash",
            name="uq_monitoring_kit_runs_idempotency",
        ),
        sa.Index(
            "ix_monitoring_kit_runs_claim",
            "scope_hash",
            "status",
            "next_wakeup_at",
            "lease_until",
        ),
        sa.Index("ix_monitoring_kit_runs_gateway", "gateway_key", "lease_until"),
        **mysql_table_options,
    )
    history_ingest = sa.Table(
        "monitoring_kit_history_ingest",
        metadata,
        sa.Column("ingest_hash", sa.String(64), primary_key=True),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.Text, nullable=False),
        sa.Column("gateway_key", sa.String(255), nullable=False),
        sa.Column("upstream_record_id", sa.Text, nullable=False),
        sa.Column("observation_fingerprint", sa.String(64), nullable=False),
        sa.Column("result_json", payload_type, nullable=False),
        sa.Index("ix_monitoring_kit_history_ingest_scope", "scope_hash"),
        **mysql_table_options,
    )
    documents = sa.Table(
        "monitoring_kit_documents",
        metadata,
        sa.Column("document_id", sa.String(128), primary_key=True),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.Text, nullable=False),
        sa.Column("subject_hash", sa.String(64), nullable=False),
        sa.Column("subject_namespace", sa.Text, nullable=False),
        sa.Column("subject_key", sa.Text, nullable=False),
        sa.Column("identity_version", sa.String(128), nullable=False),
        sa.Column("document_json", payload_type, nullable=False),
        sa.UniqueConstraint(
            "scope_hash",
            "subject_hash",
            name="uq_monitoring_kit_documents_subject",
        ),
        sa.Index("ix_monitoring_kit_documents_scope", "scope_hash"),
        **mysql_table_options,
    )
    snapshots = sa.Table(
        "monitoring_kit_snapshots",
        metadata,
        sa.Column("snapshot_id", sa.String(128), primary_key=True),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.Text, nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("snapshot_json", payload_type, nullable=False),
        sa.UniqueConstraint(
            "document_id",
            "revision",
            name="uq_monitoring_kit_snapshots_revision",
        ),
        sa.Index("ix_monitoring_kit_snapshots_document", "document_id", "revision"),
        **mysql_table_options,
    )
    events = sa.Table(
        "monitoring_kit_change_events",
        metadata,
        sa.Column("event_order", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(128), nullable=False, unique=True),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.Text, nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("event_json", payload_type, nullable=False),
        sa.UniqueConstraint(
            "document_id",
            "sequence",
            name="uq_monitoring_kit_events_sequence",
        ),
        sa.Index("ix_monitoring_kit_events_scope_order", "scope_hash", "event_order"),
        sa.Index("ix_monitoring_kit_events_document", "document_id", "sequence"),
        **mysql_table_options,
    )
    outbox = sa.Table(
        "monitoring_kit_event_outbox",
        metadata,
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.Text, nullable=False),
        sa.Column("document_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_until", sa.DateTime(timezone=False)),
        sa.Column("state_version", sa.Integer, nullable=False),
        sa.Column("event_json", payload_type, nullable=False),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_error_message", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=False)),
        sa.Index(
            "ix_monitoring_kit_outbox_claim",
            "status",
            "next_attempt_at",
            "lease_until",
            "created_at",
        ),
        sa.Index(
            "ix_monitoring_kit_outbox_document",
            "document_id",
            "sequence",
            "status",
        ),
        **mysql_table_options,
    )
    allocator_state = sa.Table(
        "monitoring_kit_allocator_state",
        metadata,
        sa.Column("component", sa.String(64), primary_key=True),
        sa.Column("last_scope_hash", sa.String(64)),
        sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False),
        **mysql_table_options,
    )
    return RelationalTables(
        metadata,
        schema_version,
        runs,
        history_ingest,
        documents,
        snapshots,
        events,
        outbox,
        allocator_state,
    )


def db_datetime(value: datetime) -> datetime:
    """将 UTC 时间以无时区值写入跨数据库兼容的 DATETIME 列。"""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("持久化时间必须带时区")
    return value.astimezone(UTC).replace(tzinfo=None)


def read_db_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise PersistenceUnavailableError("数据库返回了无法解析的时间字段")
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ensure_schema(engine: Any, tables: RelationalTables) -> None:
    """创建当前 schema，并拒绝未知的未来版本。"""

    sa = require_sqlalchemy()
    backend = engine.dialect.name
    if backend not in {"sqlite", "mysql"}:
        raise PersistenceConfigurationError(f"不支持的关系型数据库方言: {backend}")

    connection = None
    lock_name = f"{SCHEMA_NAME}:{str(getattr(engine.url, 'database', '') or '')[:40]}"
    mysql_lock_acquired = False
    try:
        connection = engine.connect()
        if backend == "sqlite":
            # SQLite 的写锁把“检查—建表—写版本”合并为一个临界区。
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            result = connection.execute(
                sa.text("SELECT GET_LOCK(:lock_name, 60)"),
                {"lock_name": lock_name},
            ).scalar()
            if result != 1:
                raise PersistenceUnavailableError("无法获取 MySQL schema 初始化锁")
            mysql_lock_acquired = True
            connection.commit()

        _ensure_schema_locked(
            connection,
            tables,
            sa,
            commit_ddl=(backend == "mysql"),
        )
        connection.commit()
    except PersistenceConfigurationError:
        _rollback_quietly(connection)
        raise
    except PersistenceUnavailableError:
        _rollback_quietly(connection)
        raise
    except Exception as exc:
        _rollback_quietly(connection)
        raise PersistenceUnavailableError("无法初始化 monitoring-kit 数据库 schema") from exc
    finally:
        if mysql_lock_acquired and connection is not None:
            try:
                connection.execute(
                    sa.text("SELECT RELEASE_LOCK(:lock_name)"),
                    {"lock_name": lock_name},
                )
                connection.commit()
            except Exception:
                _rollback_quietly(connection)
        if connection is not None:
            connection.close()


def _ensure_schema_locked(connection: Any, tables: RelationalTables, sa: Any, *, commit_ddl: bool) -> None:
    existing_version = _read_schema_version(connection, tables, sa)
    _validate_schema_version(existing_version)

    if existing_version == SCHEMA_VERSION - 1:
        _migrate_previous_schema(connection, tables, sa)

    tables.metadata.create_all(connection, checkfirst=True)
    if commit_ddl:
        connection.commit()
    _seed_allocator_state(connection, tables, sa)

    current_version = _read_schema_version(connection, tables, sa)
    if current_version is None:
        connection.execute(
            tables.schema_version.insert().values(
                component=SCHEMA_NAME,
                version=SCHEMA_VERSION,
                applied_at=db_datetime(datetime.now(UTC)),
            )
        )
    elif current_version == SCHEMA_VERSION - 1:
        connection.execute(
            sa.update(tables.schema_version)
            .where(tables.schema_version.c.component == SCHEMA_NAME)
            .values(version=SCHEMA_VERSION, applied_at=db_datetime(datetime.now(UTC)))
        )
    elif current_version != SCHEMA_VERSION:
        raise PersistenceConfigurationError(
            f"数据库 schema 版本 {current_version} 不受当前适配器支持"
        )


def _read_schema_version(connection: Any, tables: RelationalTables, sa: Any) -> int | None:
    if not sa.inspect(connection).has_table(tables.schema_version.name):
        return None
    return connection.execute(
        sa.select(tables.schema_version.c.version).where(
            tables.schema_version.c.component == SCHEMA_NAME
        )
    ).scalar()


def _validate_schema_version(version: int | None) -> None:
    if version is not None and version > SCHEMA_VERSION:
        raise PersistenceConfigurationError(
            f"数据库 schema 版本 {version} 高于适配器支持的版本 {SCHEMA_VERSION}"
        )
    if version is not None and version < SCHEMA_VERSION - 1:
        raise PersistenceConfigurationError(
            f"数据库 schema 版本 {version} 需要显式迁移到 {SCHEMA_VERSION}"
        )


def _migrate_previous_schema(connection: Any, tables: RelationalTables, sa: Any) -> None:
    """将尚未正式发布的 v1 schema 升级为当前 v2。"""

    inspector = sa.inspect(connection)
    columns = {column["name"] for column in inspector.get_columns(tables.runs.name)}
    if "gateway_key" not in columns:
        connection.execute(sa.text(f"ALTER TABLE {tables.runs.name} ADD COLUMN gateway_key VARCHAR(255)"))
    sa.Index(
        "ix_monitoring_kit_runs_gateway",
        tables.runs.c.gateway_key,
        tables.runs.c.lease_until,
    ).create(
        connection,
        checkfirst=True,
    )
    _normalize_previous_runs(connection, tables, sa)


def _normalize_previous_runs(connection: Any, tables: RelationalTables, sa: Any) -> None:
    """把 v1 中可能较长的请求指纹和缺失的网关投影升级为 v2 形状。"""

    from ....runtime.allocation import candidate_gateway
    from .codecs import decode_run, encode_run, json_dumps

    rows = connection.execute(
        sa.select(tables.runs.c.run_id, tables.runs.c.record_json)
    ).mappings().all()
    for row in rows:
        try:
            record = decode_run(row["record_json"])
            record.request_fingerprint = record.request.fingerprint()
            gateway_key = candidate_gateway(record)
        except Exception as exc:
            raise PersistenceConfigurationError(
                f"无法迁移旧 Run 载荷: {row['run_id']}"
            ) from exc
        connection.execute(
            sa.update(tables.runs)
            .where(tables.runs.c.run_id == row["run_id"])
            .values(
                request_fingerprint=record.request_fingerprint,
                gateway_key=gateway_key,
                record_json=json_dumps(encode_run(record)),
            )
        )


def _seed_allocator_state(connection: Any, tables: RelationalTables, sa: Any) -> None:
    now = db_datetime(datetime.now(UTC))
    existing = connection.execute(
        sa.select(tables.allocator_state.c.component).where(
            tables.allocator_state.c.component == "work_allocator"
        )
    ).first()
    if existing is None:
        connection.execute(
            tables.allocator_state.insert().values(
                component="work_allocator",
                last_scope_hash=None,
                updated_at=now,
            )
        )


def _rollback_quietly(connection: Any) -> None:
    if connection is None:
        return
    try:
        connection.rollback()
    except Exception:
        pass
