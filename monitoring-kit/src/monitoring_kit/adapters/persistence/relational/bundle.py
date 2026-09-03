"""关系型持久化适配器的公开装配入口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .dialects import MySQLDialect, SQLiteDialect
from .errors import PersistenceConfigurationError, PersistenceUnavailableError
from .history_store import RelationalHistoryStore
from .run_state_store import RelationalRunStateStore
from .schema import build_tables, ensure_schema, require_sqlalchemy
from ....runtime.model import DeliveryGuarantee


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    """关系型适配器配置；数据库类型由 database_url 的 scheme 决定。"""

    database_url: str
    echo: bool = False
    pool_pre_ping: bool = True
    connect_args: Mapping[str, Any] = field(default_factory=dict)
    initialize_schema: bool = True
    delivery_guarantee: DeliveryGuarantee = DeliveryGuarantee.NONE

    def __post_init__(self) -> None:
        if not isinstance(self.database_url, str) or not self.database_url.strip():
            raise PersistenceConfigurationError("database_url 必须是非空字符串")
        object.__setattr__(self, "database_url", self.database_url.strip())
        if not isinstance(self.connect_args, Mapping):
            raise PersistenceConfigurationError("connect_args 必须是对象")
        object.__setattr__(self, "connect_args", dict(self.connect_args))
        if not isinstance(self.delivery_guarantee, DeliveryGuarantee):
            raise PersistenceConfigurationError("delivery_guarantee 必须是 DeliveryGuarantee")

    @property
    def safe_url(self) -> str:
        """返回可用于诊断的脱敏连接串，绝不暴露密码。"""

        try:
            from sqlalchemy.engine import make_url

            url = make_url(self.database_url).set(query={})
            return url.render_as_string(hide_password=True)
        except Exception:
            # SQLAlchemy 未安装或 URL 不完整时，也不要原样回显潜在密码。
            return "<redacted database url>"


class PersistenceBundle:
    """同一关系型数据库上的 Store 组合与连接生命周期。"""

    def __init__(self, engine: Any, run_state_store: RelationalRunStateStore, history_store: RelationalHistoryStore) -> None:
        self.run_state_store = run_state_store
        self.history_store = history_store
        self.event_delivery_store = history_store
        self.work_allocator = run_state_store
        self._engine = engine
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._engine.dispose()
            self._closed = True

    def __enter__(self) -> "PersistenceBundle":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def open_persistence(config: PersistenceConfig) -> PersistenceBundle:
    """按 database_url 创建并初始化 SQLite/MySQL Store 组合。"""

    if not isinstance(config, PersistenceConfig):
        raise PersistenceConfigurationError("open_persistence 需要 PersistenceConfig")
    sa = require_sqlalchemy()
    try:
        url = sa.engine.make_url(config.database_url)
    except Exception as exc:
        raise PersistenceConfigurationError("database_url 不是有效的数据库连接串") from exc

    backend = url.get_backend_name()
    if backend == "sqlite":
        dialect = SQLiteDialect()
    elif backend == "mysql":
        dialect = MySQLDialect()
    else:
        raise PersistenceConfigurationError("当前关系型适配器只支持 SQLite 和 MySQL")

    connect_args = dict(config.connect_args)
    engine_options: dict[str, Any] = {
        "future": True,
        "echo": config.echo,
        "pool_pre_ping": config.pool_pre_ping,
    }
    if backend == "sqlite":
        connect_args.setdefault("check_same_thread", False)
        if _is_memory_sqlite(url):
            engine_options["poolclass"] = sa.pool.StaticPool
    engine_options["connect_args"] = connect_args

    engine = None
    try:
        engine = sa.create_engine(config.database_url, **engine_options)
        tables = build_tables()
        if config.initialize_schema:
            ensure_schema(engine, tables)
        run_store = RelationalRunStateStore(engine, tables, dialect)
        history_store = RelationalHistoryStore(
            engine,
            tables,
            dialect,
            delivery_guarantee=config.delivery_guarantee,
        )
        return PersistenceBundle(engine, run_store, history_store)
    except (sa.exc.NoSuchModuleError, ModuleNotFoundError) as exc:
        _dispose_quietly(engine)
        raise PersistenceConfigurationError("database_url 指定的数据库驱动不可用") from exc
    except (PersistenceConfigurationError, PersistenceUnavailableError):
        _dispose_quietly(engine)
        raise
    except Exception as exc:
        _dispose_quietly(engine)
        raise PersistenceUnavailableError("无法打开关系型持久化适配器") from exc


def _is_memory_sqlite(url: Any) -> bool:
    return url.database in (None, ":memory:")


def _dispose_quietly(engine: Any) -> None:
    if engine is None:
        return
    try:
        engine.dispose()
    except Exception:
        pass
