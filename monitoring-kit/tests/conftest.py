import os
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


@pytest.fixture
def mysql_persistence():
    """为真实 MySQL 契约测试提供隔离的关系型 Store 组合。

    该夹具只接受显式配置的测试数据库，并在每个测试前后清理监测库的
    业务表；不会删除 schema 版本和分配器状态表本身。
    """

    database_url = os.environ.get("MONITORING_KIT_TEST_MYSQL_URL")
    if not database_url:
        pytest.skip("设置 MONITORING_KIT_TEST_MYSQL_URL 后运行真实 MySQL 契约")

    pytest.importorskip("sqlalchemy")
    from monitoring_kit.adapters.persistence.relational import PersistenceConfig, open_persistence
    from monitoring_kit.runtime import DeliveryGuarantee

    bundle = open_persistence(
        PersistenceConfig(
            database_url,
            delivery_guarantee=DeliveryGuarantee.AT_LEAST_ONCE,
        )
    )
    try:
        _clear_mysql_test_data(bundle)
        yield bundle
    finally:
        try:
            _clear_mysql_test_data(bundle)
        finally:
            bundle.close()


def _clear_mysql_test_data(bundle) -> None:
    """清理显式测试库中的数据，保留 schema 和 allocator 初始化记录。"""

    import sqlalchemy as sa

    tables = bundle.run_state_store._tables
    with bundle._engine.begin() as connection:
        for table in (
            tables.outbox,
            tables.events,
            tables.snapshots,
            tables.documents,
            tables.history_ingest,
            tables.runs,
        ):
            connection.execute(table.delete())
        connection.execute(
            sa.update(tables.allocator_state)
            .where(tables.allocator_state.c.component == "work_allocator")
            .values(last_scope_hash=None)
        )
