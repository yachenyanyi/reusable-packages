from datetime import UTC, datetime

from monitoring_kit.contracts import (
    ChangeKind,
    Document,
    IngestKey,
    Observation,
    Presence,
    Provenance,
    SubjectRef,
    Snapshot,
    TypedEnvelope,
)
from monitoring_kit.history.history import ContentHistory
from monitoring_kit.runtime.registry import ExtensionRegistry

from tests.support import TestContentPolicy, build_engine


def test_same_subject_creates_revision_but_volatile_field_does_not():
    engine, _, history_store, _, _, _ = build_engine(
        [
            {"record_id": "r1", "payload": {"key": "same", "body": "v1", "volatile": 1}},
            {"record_id": "r2", "payload": {"key": "same", "body": "v1", "volatile": 2}},
            {"record_id": "r3", "payload": {"key": "same", "body": "v2", "volatile": 3}},
        ],
    )
    from tests.support import context, request

    ref = engine.submit_run(request(), context())
    engine.wake()
    summary = engine.get_run(ref.run_id, "scope-a")
    assert summary.processed_count == 3
    assert summary.change_count == 2
    from monitoring_kit.contracts import ChangeQuery

    changes = engine.query_changes(ChangeQuery(limit=10), "scope-a")
    assert [event.kind for event in changes.events] == [ChangeKind.FIRST_SEEN, ChangeKind.REVISED]
    document_id = changes.events[0].document_id
    timeline = history_store.get_timeline("scope-a", document_id)
    assert timeline is not None
    assert len(timeline.snapshots) == 2
    assert Document.from_dict(timeline.document.to_dict()) == timeline.document
    assert Snapshot.from_dict(timeline.snapshots[0].to_dict()) == timeline.snapshots[0]


def test_missing_confirmation_and_restore_are_policy_decisions():
    engine, _, history_store, _, _, clock = build_engine(
        [
            {"record_id": "present", "payload": {"key": "gone", "body": "v1"}},
            {"record_id": "missing-1", "payload": {"key": "gone"}, "deleted": True},
            {"record_id": "missing-2", "payload": {"key": "gone"}, "deleted": True},
            {"record_id": "restored", "payload": {"key": "gone", "body": "v1"}},
        ],
    )
    from tests.support import context, request

    engine.submit_run(request(), context("missing-run"))
    engine.wake()
    from monitoring_kit.contracts import ChangeQuery

    changes = engine.query_changes(ChangeQuery(limit=10), "scope-a")
    assert [event.kind for event in changes.events] == [
        ChangeKind.FIRST_SEEN,
        ChangeKind.MISSING_SUSPECTED,
        ChangeKind.MISSING_CONFIRMED,
        ChangeKind.RESTORED,
    ]
    view = history_store.get_current("scope-a", changes.events[0].document_id)
    assert view is not None
    assert view.document.state.value == "present"
    assert view.document.missing_streak == 0


def test_history_ingest_idempotency_returns_same_result_without_new_event():
    engine, _, history_store, _, _, clock = build_engine([])
    registry = ExtensionRegistry()
    registry.register_content_policy(TestContentPolicy())
    history = ContentHistory(history_store, registry, clock=clock)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    subject = SubjectRef("test.content", "key", "1.0")
    observation = Observation(
        observation_id="obs-1",
        scope_key="scope-a",
        run_id="run-1",
        ingest_key=IngestKey("gateway", "record-1"),
        subject=subject,
        observed_at=now,
        presence=Presence.PRESENT,
        content=TypedEnvelope("test.content", "1.0", {"key": "key", "body": "body"}),
        provenance=Provenance(),
    )
    first = history.record(observation)
    duplicate = history.record(
        Observation(
            observation_id="different-id",
            scope_key=observation.scope_key,
            run_id=observation.run_id,
            ingest_key=observation.ingest_key,
            subject=observation.subject,
            observed_at=observation.observed_at,
            presence=observation.presence,
            content=observation.content,
            provenance=observation.provenance,
        )
    )
    assert first.events[0].event_id == duplicate.events[0].event_id
    assert duplicate.duplicate is True
    assert len(history_store.get_timeline("scope-a", first.document.document_id).events) == 1


def test_same_ingest_key_can_be_replayed_from_another_run_without_new_history():
    engine, _, history_store, _, _, clock = build_engine([])
    registry = ExtensionRegistry()
    registry.register_content_policy(TestContentPolicy())
    history = ContentHistory(history_store, registry, clock=clock)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    content = TypedEnvelope("test.content", "1.0", {"key": "key", "body": "body"})
    first = Observation(
        observation_id="obs-1",
        scope_key="scope-a",
        run_id="run-1",
        ingest_key=IngestKey("gateway", "record-1"),
        subject=SubjectRef("test.content", "key", "1.0"),
        observed_at=now,
        presence=Presence.PRESENT,
        content=content,
        provenance=Provenance(upstream_job_ref="job-1", attempt_ref="attempt-1", collector_ref="worker-1"),
    )
    replay = Observation(
        observation_id="obs-2",
        scope_key="scope-a",
        run_id="run-2",
        ingest_key=first.ingest_key,
        subject=first.subject,
        observed_at=now,
        presence=Presence.PRESENT,
        content=content,
        provenance=Provenance(upstream_job_ref="job-2", attempt_ref="attempt-2", collector_ref="worker-2"),
    )
    first_result = history.record(replay)
    assert history.record(first).duplicate is True
    assert first_result.duplicate is False
    timeline = history_store.get_timeline("scope-a", first_result.document.document_id)
    assert timeline is not None
    assert len(timeline.events) == 1
