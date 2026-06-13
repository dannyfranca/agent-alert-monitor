from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_alert_monitor.alert import parse_alert_text
from agent_alert_monitor.cli import main
from agent_alert_monitor.ledger import AlertLedger


def test_cli_synthetic_alert_dry_run_can_select_project(tmp_path: Path, capsys) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    config_file.write_text(
        f"""
runtime:
  state_dir: {state_dir}
projects:
  - slug: alpha-api
    display_name: Alpha API
    telegram:
      bot_token_env: ALERT_MONITOR_ALPHA_BOT_TOKEN
      alert_chat_id: "-100111"
    hermes:
      coordinator_profile: alpha-coordinator
    kanban:
      tenant: alpha
      incident_assignee: alpha-debugger
  - slug: beta-worker
    display_name: Beta Worker
    telegram:
      bot_token_env: ALERT_MONITOR_BETA_BOT_TOKEN
      alert_chat_id: "-100222"
    hermes:
      coordinator_profile: beta-coordinator
    kanban:
      tenant: beta
      incident_assignee: beta-debugger
""".strip(),
        encoding="utf-8",
    )

    code = main(
        [
            "--config",
            str(config_file),
            "--project",
            "beta-worker",
            "synthetic-alert",
            "--message-id",
            "synthetic-1",
            "--text",
            "CRITICAL ALARM: QueueDepth service=worker",
            "--dry-run",
        ],
        env={
            "ALERT_MONITOR_ALPHA_BOT_TOKEN": "alpha-token",
            "ALERT_MONITOR_BETA_BOT_TOKEN": "beta-token",
        },
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_card"]["assignee"] == "beta-debugger"
    assert payload["planned_card"]["tenant"] == "beta"
    assert "Beta Worker" in payload["planned_card"]["title"]
    assert "telegram alert channel -100222/synthetic-1" in payload["planned_card"]["body"]


def test_incident_update_rejects_closed_status_without_final_channel_status(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    config_file.write_text(
        f"""
telegram:
  bot_token_env: ALERT_MONITOR_TEST_BOT_TOKEN
  alert_chat_id: "-100123"
hermes:
  coordinator_profile: alert-coordinator
kanban:
  incident_assignee: debugger
runtime:
  state_dir: {state_dir}
""".strip(),
        encoding="utf-8",
    )
    ledger = AlertLedger(state_dir / "ledger.sqlite")
    parsed = parse_alert_text("ALARM: Service5xx service=api")
    ledger.open_incident("t_incident", "service5xx", parsed, "investigating")

    with pytest.raises(ValueError, match="final channel status"):
        main(
            [
                "--config",
                str(config_file),
                "incident-update",
                "--incident",
                "t_incident",
                "--status",
                "resolved",
            ],
            env={"ALERT_MONITOR_TEST_BOT_TOKEN": "token"},
        )

    incident = ledger.get_incident("t_incident")
    assert incident is not None
    assert incident.status == "investigating"


def test_cli_synthetic_alert_dry_run_has_no_external_side_effects(tmp_path: Path, capsys) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    config_file.write_text(
        f"""
telegram:
  bot_token_env: ALERT_MONITOR_TEST_BOT_TOKEN
  alert_chat_id: "-100123"
hermes:
  coordinator_profile: alert-coordinator
kanban:
  incident_assignee: debugger
runtime:
  state_dir: {state_dir}
""".strip(),
        encoding="utf-8",
    )

    code = main(
        [
            "--config",
            str(config_file),
            "synthetic-alert",
            "--message-id",
            "synthetic-1",
            "--text",
            "CRITICAL ALARM: Service5xx service=api",
            "--dry-run",
        ],
        env={"ALERT_MONITOR_TEST_BOT_TOKEN": "token"},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "would_create_incident"
    assert payload["external_side_effects"] is False
    assert payload["planned_card"]["assignee"] == "debugger"
    assert not state_dir.exists()
