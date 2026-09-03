import json

from monitoring_kit.cli import main


def test_cli_demo_is_a_black_box_core_acceptance(capsys):
    assert main(["demo"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["status"] == "completed"
    assert payload["changes"] == ["first_seen", "revised"]
