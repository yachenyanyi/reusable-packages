from datetime import UTC, datetime, timedelta

import pytest

from monitoring_kit.contracts import (
    ChangeEvent,
    ChangeKind,
    ExecutionContext,
    IngestKey,
    Observation,
    Presence,
    Provenance,
    RequestedWindow,
    RunRequest,
    SubjectRef,
    TypedEnvelope,
)


def test_typed_envelope_copies_json_data_and_rejects_invalid_type():
    data = {"nested": {"value": 1}}
    envelope = TypedEnvelope("org.example.content", "1.0", data)
    data["nested"]["value"] = 2
    assert envelope.data["nested"]["value"] == 1
    assert envelope.to_dict()["data"]["nested"]["value"] == 1
    with pytest.raises(ValueError):
        TypedEnvelope("not a type", "1.0", {})


def test_run_request_and_context_validate_time_and_identity_fields():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    request = RunRequest(
        TypedEnvelope("org.example.collection", "1.0", {"key": "value"}),
        requested_window=RequestedWindow(now, now + timedelta(hours=1)),
    )
    assert request.fingerprint() == request.fingerprint()
    assert RunRequest.from_dict(request.to_dict()) == request
    assert ExecutionContext("scope", "actor", "idem").scope_key == "scope"
    with pytest.raises(ValueError):
        RequestedWindow(now, now)


def test_observation_presence_is_explicit():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    base = dict(
        observation_id="obs-1",
        scope_key="scope",
        run_id="run-1",
        ingest_key=IngestKey("gateway", "record"),
        subject=SubjectRef("org.example.content", "key", "1.0"),
        observed_at=now,
        provenance=Provenance(),
    )
    with pytest.raises(ValueError):
        Observation(**base, presence=Presence.PRESENT, content=None)
    with pytest.raises(ValueError):
        Observation(
            **base,
            presence=Presence.ABSENT,
            content=TypedEnvelope("org.example.content", "1.0", {"x": 1}),
        )


def test_observation_round_trips_through_json_shape():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    observation = Observation(
        observation_id="obs-1",
        scope_key="scope",
        run_id="run-1",
        ingest_key=IngestKey("gateway", "record"),
        subject=SubjectRef("org.example.content", "key", "1.0"),
        observed_at=now,
        presence=Presence.PRESENT,
        content=TypedEnvelope("org.example.content", "1.0", {"body": "text"}),
        provenance=Provenance(raw_artifact_ref="artifact-1"),
        published_at=now,
        received_at=now,
    )
    assert Observation.from_dict(observation.to_dict()) == observation


def test_change_event_round_trips_through_json_shape():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    event = ChangeEvent(
        event_id="event-1",
        scope_key="scope",
        document_id="doc-1",
        run_id="run-1",
        sequence=1,
        kind=ChangeKind.FIRST_SEEN,
        occurred_at=now,
        effective_observed_at=now,
        to_snapshot_id="snapshot-1",
        evidence_refs=("artifact-1",),
        policy_ref="policy@1.0",
        details=TypedEnvelope("org.example.details", "1.0", {"reason": "new"}),
    )
    assert ChangeEvent.from_dict(event.to_dict()) == event


def test_change_event_enforces_first_seen_and_revision_references():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    common = dict(
        event_id="event-1",
        scope_key="scope",
        document_id="doc-1",
        run_id="run-1",
        sequence=1,
        occurred_at=now,
        effective_observed_at=now,
        policy_ref="policy@1.0",
    )
    with pytest.raises(ValueError):
        ChangeEvent(**common, kind=ChangeKind.FIRST_SEEN)
    with pytest.raises(ValueError):
        ChangeEvent(**common, kind=ChangeKind.REVISED, from_snapshot_id="one")
