from monitoring_kit.adapters.events.memory import InMemoryAuditSink, InMemoryTelemetrySink
from monitoring_kit.contracts import RunStatus

from tests.support import build_engine, context, request


def test_audit_and_telemetry_hooks_are_optional_and_non_domain_specific():
    audit = InMemoryAuditSink()
    telemetry = InMemoryTelemetrySink()
    engine, _, _, _, _, _ = build_engine(
        [{"record_id": "one", "payload": {"key": "one", "body": "body"}}],
        audit_sink=audit,
        telemetry_sink=telemetry,
    )
    ref = engine.submit_run(request(), context("hooks"))
    engine.wake()
    assert engine.get_run(ref.run_id, "scope-a").status is RunStatus.COMPLETED
    assert [event.action for event in audit.events] == ["run.accepted", "run.finished"]
    assert {item[0] for item in telemetry.measurements} == {
        "monitoring.run.accepted",
        "monitoring.run.finished",
    }
