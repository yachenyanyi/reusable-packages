from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import threading
from tempfile import TemporaryDirectory

import pytest

sa = pytest.importorskip("sqlalchemy")

from monitoring_kit.adapters.persistence.relational import (
    PersistenceConfig,
    PersistenceConfigurationError,
    PersistenceInvariantError,
    open_persistence,
)
from monitoring_kit.adapters.persistence.relational.dialects.mysql import MySQLDialect
from monitoring_kit.adapters.persistence.relational.schema import (
    build_tables,
    db_datetime,
    ensure_schema,
)
from monitoring_kit.collection.engine import CollectionEngine, RetryPolicy
from monitoring_kit.collection.model import RunRecord, UpstreamJobRequest
from monitoring_kit.collection.ports import RunStateConflictError
from monitoring_kit.contracts import (
    ChangeKind,
    ChangeQuery,
    IngestKey,
    RunRequest,
    RunStatus,
    TypedEnvelope,
)
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.runtime.registry import ExtensionRegistry
from monitoring_kit.adapters.persistence.relational.codecs import (
    encode_run,
    idempotency_hash,
    json_dumps,
    scope_hash,
)

from tests.support import ManualClock, ScriptedGateway, TestCollectionAdapter, TestContentPolicy, context, request


def _build_engine(bundle, records, *, clock=None):
    clock = clock or ManualClock()
    registry = ExtensionRegistry()
    registry.register_collection_adapter(TestCollectionAdapter())
    registry.register_content_policy(TestContentPolicy())
    history = ContentHistory(bundle.history_store, registry, clock=clock)
    gateway = ScriptedGateway(records)
    engine = CollectionEngine(
        bundle.run_state_store,
        history,
        registry,
        gateway,
        clock=clock,
        retry_policy=RetryPolicy(poll_interval_seconds=0, lease_seconds=30),
        worker_id="sqlite-test-worker",
    )
    return engine, history, gateway, clock


def test_sqlite_persistence_round_trips_run_and_history_after_reopen():
    with TemporaryDirectory() as directory:
        database_url = f"sqlite:///{Path(directory, 'monitoring.db').as_posix()}"
        bundle = open_persistence(PersistenceConfig(database_url))
        engine, _, _, _ = _build_engine(
            bundle,
            [{"record_id": "r1", "payload": {"key": "key", "body": "v1"}}],
        )
        ref = engine.submit_run(request(), context("round-trip"))
        engine.wake()
        assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.COMPLETED
        events = engine.query_changes(ChangeQuery(limit=10), "scope-a").events
        assert len(events) == 1
        document_id = events[0].document_id
        bundle.close()

        reopened = open_persistence(PersistenceConfig(database_url))
        try:
            restored = reopened.run_state_store.get("scope-a", ref.run_id)
            assert restored is not None
            assert restored.status is RunStatus.COMPLETED
            assert restored.processed_count == 1
            assert len(restored.attempts) == 3
            timeline = reopened.history_store.get_timeline("scope-a", document_id)
            assert timeline is not None
            assert len(timeline.snapshots) == 1
            assert len(timeline.events) == 1
            assert timeline.events[0].kind.value == "first_seen"
        finally:
            reopened.close()


def test_relational_history_replay_is_idempotent():
    with TemporaryDirectory() as directory:
        bundle = open_persistence(PersistenceConfig(f"sqlite:///{Path(directory, 'history.db').as_posix()}"))
        try:
            # 通过一次真实引擎运行拿到已规范化的 Observation，再重放它。
            engine, history, _, _ = _build_engine(
                bundle,
                [{"record_id": "r1", "payload": {"key": "key", "body": "v1"}}],
            )
            ref = engine.submit_run(request(), context("history-replay"))
            engine.wake()
            document_id = engine.query_changes(ChangeQuery(limit=10), "scope-a").events[0].document_id
            stored = bundle.history_store.get_timeline("scope-a", document_id)
            assert stored is not None
            observation = stored.events[0].run_id  # 确认事件来自持久化运行
            assert observation == ref.run_id
            # 直接从已保存的 HistoryResult 重放，接口只暴露端口允许的结果属性。
            history_result = bundle.history_store.get_by_ingest_key(
                "scope-a",
                IngestKey("scripted", "r1"),
            )
            assert history_result is not None
            duplicate = history.record(history_result.result.observation)
            assert duplicate.duplicate is True
            assert len(bundle.history_store.get_timeline("scope-a", document_id).events) == 1
        finally:
            bundle.close()


def test_relational_history_commit_rolls_back_all_rows_on_invariant_failure():
    with TemporaryDirectory() as directory:
        bundle = open_persistence(PersistenceConfig(f"sqlite:///{Path(directory, 'atomic.db').as_posix()}"))
        try:
            engine, _, _, _ = _build_engine(
                bundle,
                [{"record_id": "r1", "payload": {"key": "key", "body": "v1"}}],
            )
            ref = engine.submit_run(request(), context("atomic-source"))
            engine.wake()
            stored = bundle.history_store.get_by_ingest_key("scope-a", IngestKey("scripted", "r1"))
            assert stored is not None
            bad_observation = replace(
                stored.result.observation,
                observation_id="bad-observation",
                ingest_key=IngestKey("scripted", "bad-r1"),
            )
            bad_snapshot = replace(
                stored.result.snapshot,
                snapshot_id="bad-snapshot",
                revision=2,
                observation_id="bad-observation",
            )
            bad_document = replace(stored.result.document, current_snapshot_id="bad-snapshot")
            # 让 Snapshot 先满足版本约束，再用重复 event_id 触发数据库约束；
            # 这样能证明已经写入事务的 Snapshot 也会随失败一起回滚。
            bad_event = replace(
                stored.result.events[0],
                sequence=2,
                to_snapshot_id="bad-snapshot",
            )
            bad_result = replace(
                stored.result,
                observation=bad_observation,
                document=bad_document,
                snapshot=bad_snapshot,
                events=(bad_event,),
            )
            from monitoring_kit.history.model import HistoryWrite

            with pytest.raises(PersistenceInvariantError):
                bundle.history_store.commit(
                    HistoryWrite(
                        ("scripted", "bad-r1"),
                        "bad-fingerprint",
                        bad_result,
                        stored.result.document,
                    )
                )
            assert bundle.history_store.get_by_ingest_key("scope-a", IngestKey("scripted", "bad-r1")) is None
            assert len(bundle.history_store.get_timeline("scope-a", stored.result.document.document_id).events) == 1
            assert ref.run_id == stored.result.observation.run_id
        finally:
            bundle.close()


def test_sqlite_claim_is_exclusive_across_independent_connections():
    with TemporaryDirectory() as directory:
        database_url = f"sqlite:///{Path(directory, 'claim.db').as_posix()}"
        first = open_persistence(PersistenceConfig(database_url))
        second = open_persistence(PersistenceConfig(database_url))
        try:
            engine, _, _, clock = _build_engine(first, [])
            ref = engine.submit_run(request(), context("claim"))
            now = clock.now()

            def claim(bundle, owner):
                return bundle.run_state_store.claim_runnable(now, 1, owner, 60)

            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(
                    workers.map(
                        lambda args: claim(*args),
                        ((first, "worker-a"), (second, "worker-b")),
                    )
                )
            assert sorted(len(items) for items in results) == [0, 1]
            claimed = next(items[0] for items in results if items)
            assert claimed.run_id == ref.run_id
        finally:
            first.close()
            second.close()


def test_schema_future_version_is_rejected_without_silent_downgrade():
    with TemporaryDirectory() as directory:
        database_url = f"sqlite:///{Path(directory, 'schema.db').as_posix()}"
        bundle = open_persistence(PersistenceConfig(database_url))
        bundle.close()
        engine = sa.create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE monitoring_kit_schema_version SET version = 999 "
                        "WHERE component = 'monitoring-kit'"
                    )
                )
        finally:
            engine.dispose()
        with pytest.raises(PersistenceConfigurationError, match="高于"):
            open_persistence(PersistenceConfig(database_url))


def test_v1_schema_migration_normalizes_legacy_run_fingerprint_and_gateway_projection(tmp_path):
    database_url = f"sqlite:///{(tmp_path / 'schema-v1.db').as_posix()}"
    engine = sa.create_engine(database_url)
    now = ManualClock().now()
    request_value = request("legacy")
    legacy_context = context("legacy-run", "scope-a")
    record = RunRecord(
        run_id="run-legacy",
        scope_key="scope-a",
        request=request_value,
        context=legacy_context,
        request_fingerprint=json_dumps(request_value.to_dict()),
        adapter_key="test-collection-adapter",
        upstream_request=UpstreamJobRequest(
            collection=request_value.collection,
            source_ref=request_value.source_ref,
            gateway_hint="gateway-a",
        ),
        status=RunStatus.QUEUED,
        accepted_at=now,
        updated_at=now,
        next_wakeup_at=now,
    )
    old_metadata = sa.MetaData()
    old_schema_version = sa.Table(
        "monitoring_kit_schema_version",
        old_metadata,
        sa.Column("component", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("applied_at", sa.DateTime, nullable=False),
    )
    old_runs = sa.Table(
        "monitoring_kit_runs",
        old_metadata,
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("scope_key", sa.Text, nullable=False),
        sa.Column("idempotency_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("next_wakeup_at", sa.DateTime),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_until", sa.DateTime),
        sa.Column("state_version", sa.Integer, nullable=False),
        sa.Column("record_json", sa.Text, nullable=False),
        sa.UniqueConstraint("scope_hash", "idempotency_hash", name="uq_monitoring_kit_runs_idempotency"),
    )
    old_metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            old_schema_version.insert().values(
                component="monitoring-kit",
                version=1,
                applied_at=db_datetime(now),
            )
        )
        connection.execute(
            old_runs.insert().values(
                run_id=record.run_id,
                scope_hash=scope_hash(record.scope_key),
                scope_key=record.scope_key,
                idempotency_hash=idempotency_hash(record.scope_key, legacy_context.idempotency_key),
                idempotency_key=legacy_context.idempotency_key,
                request_fingerprint=record.request_fingerprint,
                status=record.status.value,
                accepted_at=db_datetime(record.accepted_at),
                updated_at=db_datetime(record.updated_at),
                next_wakeup_at=db_datetime(record.next_wakeup_at),
                lease_owner=None,
                lease_until=None,
                state_version=0,
                record_json=json_dumps(encode_run(record)),
            )
        )
    ensure_schema(engine, build_tables())
    engine.dispose()

    bundle = open_persistence(PersistenceConfig(database_url))
    try:
        migrated = bundle.run_state_store.get("scope-a", record.run_id)
        assert migrated is not None
        assert migrated.request_fingerprint == request_value.fingerprint()
        assert migrated.upstream_request.gateway_hint == "gateway-a"
        with bundle._engine.connect() as connection:
            version = connection.execute(
                sa.text(
                    "SELECT version FROM monitoring_kit_schema_version "
                    "WHERE component = 'monitoring-kit'"
                )
            ).scalar_one()
        assert version == 2
    finally:
        bundle.close()


def test_persistence_config_masks_password_and_rejects_unknown_backend():
    config = PersistenceConfig("mysql+pymysql://user:secret@example.test:3306/monitoring")
    assert "secret" not in config.safe_url
    assert "***" in config.safe_url
    with pytest.raises(PersistenceConfigurationError):
        open_persistence(PersistenceConfig("postgresql://user:secret@example.test/db"))


def test_missing_mysql_driver_is_reported_as_configuration_error():
    with pytest.raises(PersistenceConfigurationError, match="驱动"):
        open_persistence(
            PersistenceConfig("mysql+driver_that_does_not_exist://user:secret@example.test/monitoring")
        )


def test_mysql_claim_strategy_compiles_with_row_lock_without_leaking_to_core():
    url = sa.engine.make_url("mysql+pymysql://user:secret@example.test/monitoring")
    engine = sa.create_mock_engine(str(url), lambda *_args, **_kwargs: None)
    statement = sa.select(build_tables().runs).limit(1)
    compiled = str(MySQLDialect().lock_rows(statement).compile(dialect=engine.dialect))
    assert "FOR UPDATE SKIP LOCKED" in compiled


def test_mysql_allocator_state_lock_waits_instead_of_skipping_the_fairness_cursor():
    url = sa.engine.make_url("mysql+pymysql://user:secret@example.test/monitoring")
    engine = sa.create_mock_engine(str(url), lambda *_args, **_kwargs: None)
    statement = sa.select(build_tables().allocator_state).limit(1)
    compiled = str(MySQLDialect().lock_state(statement).compile(dialect=engine.dialect))
    assert "FOR UPDATE" in compiled
    assert "SKIP LOCKED" not in compiled


def test_run_request_fingerprint_is_a_fixed_sha256_digest():
    run_request = RunRequest(
        collection=TypedEnvelope("test.collection", "1.0", {"records": ["one"]}),
        source_ref="test-source",
        correlation_refs={"large": "x" * 300},
    )

    fingerprint = run_request.fingerprint()

    assert len(fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in fingerprint)


def test_mysql_schema_can_hold_large_json_payloads_and_declares_storage_defaults():
    from sqlalchemy.dialects.mysql import LONGTEXT
    from sqlalchemy.schema import CreateTable

    tables = build_tables()
    mysql_dialect = sa.dialects.mysql.dialect()
    for table_name, column_name in (
        ("runs", "record_json"),
        ("history_ingest", "result_json"),
        ("documents", "document_json"),
        ("snapshots", "snapshot_json"),
        ("events", "event_json"),
    ):
        column = getattr(tables, table_name).c[column_name]
        assert isinstance(column.type.dialect_impl(mysql_dialect), LONGTEXT)

    ddl = str(CreateTable(tables.runs).compile(dialect=mysql_dialect))
    assert "ENGINE=InnoDB" in ddl
    assert "CHARSET=utf8mb4" in ddl
    assert tables.runs.c.request_fingerprint.type.length == 64


def test_expired_worker_cannot_save_after_another_worker_reclaims_run():
    with TemporaryDirectory() as directory:
        database_url = f"sqlite:///{Path(directory, 'lease-fence.db').as_posix()}"
        first = open_persistence(PersistenceConfig(database_url))
        second = open_persistence(PersistenceConfig(database_url))
        try:
            engine, _, _, clock = _build_engine(first, [])
            ref = engine.submit_run(request(), context("lease-fence"))
            old_copy = first.run_state_store.claim_runnable(clock.now(), 1, "worker-a", 10)[0]

            clock.advance(11)
            current_copy = second.run_state_store.claim_runnable(clock.now(), 1, "worker-b", 10)[0]
            assert current_copy.run_id == ref.run_id
            assert current_copy.lease_owner == "worker-b"

            old_copy.cursor = "stale-cursor"
            with pytest.raises(RunStateConflictError):
                first.run_state_store.save(old_copy)

            restored = second.run_state_store.get("scope-a", ref.run_id)
            assert restored is not None
            assert restored.lease_owner == "worker-b"
            assert restored.cursor is None
        finally:
            first.close()
            second.close()


def test_history_concurrent_stale_no_change_cannot_move_current_snapshot_back():
    with TemporaryDirectory() as directory:
        database_url = f"sqlite:///{Path(directory, 'history-fence.db').as_posix()}"
        bundle = open_persistence(PersistenceConfig(database_url))
        try:
            engine, history_a, _, _ = _build_engine(
                bundle,
                [{"record_id": "initial", "payload": {"key": "same", "body": "v1"}}],
            )
            engine.submit_run(request(), context("history-fence-initial"))
            engine.wake()
            stored = bundle.history_store.get_by_ingest_key(
                "scope-a", IngestKey("scripted", "initial")
            )
            assert stored is not None
            initial = stored.result.observation

            registry = ExtensionRegistry()
            registry.register_collection_adapter(TestCollectionAdapter())
            registry.register_content_policy(TestContentPolicy())

            class BlockingStore:
                def __init__(self, delegate):
                    self.delegate = delegate
                    self.started = threading.Event()
                    self.release = threading.Event()
                    self.blocked = False

                def __getattr__(self, name):
                    return getattr(self.delegate, name)

                def commit(self, write):
                    if not self.blocked:
                        self.blocked = True
                        self.started.set()
                        assert self.release.wait(5), "等待并发历史提交超时"
                    return self.delegate.commit(write)

            blocking_store = BlockingStore(bundle.history_store)
            history_b = ContentHistory(blocking_store, registry, clock=engine._clock)
            stale = replace(
                initial,
                observation_id="stale-observation",
                run_id="stale-run",
                ingest_key=IngestKey("scripted", "stale"),
            )
            newer = replace(
                initial,
                observation_id="newer-observation",
                run_id="newer-run",
                ingest_key=IngestKey("scripted", "newer"),
                content=TypedEnvelope("test.content", "1.0", {"key": "same", "body": "v2"}),
            )
            errors = []

            def record_stale():
                try:
                    history_b.record(stale)
                except Exception as exc:  # 由断言统一报告，避免线程吞掉失败
                    errors.append(exc)

            worker = threading.Thread(target=record_stale)
            worker.start()
            assert blocking_store.started.wait(5), "并发历史提交没有进入等待点"
            history_a.record(newer)
            blocking_store.release.set()
            worker.join(5)
            assert not worker.is_alive(), "并发历史提交线程没有结束"
            if errors:
                raise errors[0]

            timeline = bundle.history_store.get_timeline("scope-a", stored.result.document.document_id)
            assert timeline is not None
            assert timeline.document.current_snapshot_id == timeline.snapshots[-1].snapshot_id
            assert timeline.snapshots[-1].content.data["body"] == "v1"
        finally:
            bundle.close()


def test_sqlite_schema_initialization_is_safe_for_concurrent_openers():
    with TemporaryDirectory() as directory:
        database_url = f"sqlite:///{Path(directory, 'concurrent-schema.db').as_posix()}"
        barrier = threading.Barrier(8)

        def open_and_close():
            barrier.wait()
            try:
                bundle = open_persistence(PersistenceConfig(database_url))
            except Exception as exc:
                return exc
            bundle.close()
            return None

        with ThreadPoolExecutor(max_workers=8) as workers:
            results = list(workers.map(lambda _: open_and_close(), range(8)))

        assert results == [None] * 8, [repr(result) for result in results if result is not None]


def test_change_query_pushes_filters_and_limit_into_sql():
    with TemporaryDirectory() as directory:
        bundle = open_persistence(
            PersistenceConfig(f"sqlite:///{Path(directory, 'query-limit.db').as_posix()}")
        )
        try:
            engine, _, _, _ = _build_engine(
                bundle,
                [
                    {"record_id": "r1", "payload": {"key": "same", "body": "v1"}},
                    {"record_id": "r2", "payload": {"key": "same", "body": "v2"}},
                    {"record_id": "r3", "payload": {"key": "same", "body": "v3"}},
                ],
            )
            engine.submit_run(request(), context("query-limit"))
            engine.wake()
            all_events = engine.query_changes(ChangeQuery(limit=10), "scope-a").events
            document_id = all_events[0].document_id
            statements = []

            def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
                if statement.lstrip().upper().startswith("SELECT") and "monitoring_kit_change_events" in statement:
                    statements.append(statement.upper())

            sa.event.listen(bundle._engine, "before_cursor_execute", capture)
            try:
                page = bundle.history_store.query_changes(
                    "scope-a",
                    ChangeQuery(
                        limit=1,
                        document_id=document_id,
                        kinds=frozenset({ChangeKind.REVISED}),
                    ),
                )
                page2 = bundle.history_store.query_changes(
                    "scope-a",
                    ChangeQuery(
                        cursor=page.next_cursor,
                        limit=1,
                        document_id=document_id,
                        kinds=frozenset({ChangeKind.REVISED}),
                    ),
                )
            finally:
                sa.event.remove(bundle._engine, "before_cursor_execute", capture)

            assert len(page.events) == 1
            assert page.has_more is True
            assert len(page2.events) == 1
            assert page2.events[0].sequence > page.events[0].sequence
            assert statements
            assert all("LIMIT" in statement for statement in statements)
            assert all("EVENT_ORDER" in statement and "KIND" in statement for statement in statements)
        finally:
            bundle.close()


def test_safe_url_removes_sensitive_query_parameters():
    config = PersistenceConfig(
        "mysql+pymysql://user:secret@example.test:3306/monitoring"
        "?password=query-secret&token=query-token&charset=utf8mb4"
    )

    safe_url = config.safe_url

    assert "secret" not in safe_url
    assert "query-token" not in safe_url
    assert "password" not in safe_url
    assert "?" not in safe_url


def test_corrupt_document_payload_is_reported_as_stable_persistence_error():
    with TemporaryDirectory() as directory:
        bundle = open_persistence(
            PersistenceConfig(f"sqlite:///{Path(directory, 'corrupt-document.db').as_posix()}")
        )
        try:
            engine, _, _, _ = _build_engine(
                bundle,
                [{"record_id": "r1", "payload": {"key": "key", "body": "v1"}}],
            )
            engine.submit_run(request(), context("corrupt-document"))
            engine.wake()
            stored = bundle.history_store.get_by_ingest_key(
                "scope-a", IngestKey("scripted", "r1")
            )
            assert stored is not None
            with bundle._engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "UPDATE monitoring_kit_documents SET document_json = :payload "
                        "WHERE document_id = :document_id"
                    ),
                    {"payload": "{}", "document_id": stored.result.document.document_id},
                )

            with pytest.raises(PersistenceInvariantError, match="Document"):
                bundle.history_store.get_document("scope-a", stored.result.document.subject)
        finally:
            bundle.close()
