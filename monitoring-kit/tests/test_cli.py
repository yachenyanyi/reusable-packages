import json

from monitoring_kit.cli import main


def test_cli_demo_is_a_black_box_core_acceptance(capsys):
    assert main(["demo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["status"] == "completed"
    assert payload["changes"] == ["first_seen", "revised"]


def test_cli_dispatch_is_a_black_box_outbox_acceptance(capsys):
    assert main(["dispatch"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["first_dispatch"]["retried"] == 1
    assert payload["second_dispatch"]["delivered"] == 1
    assert payload["published_event_ids"] == [payload["event_id"]]
