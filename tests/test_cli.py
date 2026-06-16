from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_alert_monitor.alert import parse_alert_text
from agent_alert_monitor.cli import main
from agent_alert_monitor.ledger import AlertLedger


def test_cli_setup_runs_without_existing_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, bool, bool]] = []

    def fake_setup(*, root: Path, validate_live: bool, force: bool):
        calls.append((root, validate_live, force))

        class Result:
            checks_failed = 0

        return Result()

    monkeypatch.setattr("agent_alert_monitor.setup_wizard.run_setup_wizard", fake_setup)

    code = main(["setup", "--root", str(tmp_path), "--skip-live-checks", "--force"])

    assert code == 0
    assert calls == [(tmp_path, False, True)]


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


def test_cli_ingest_skips_v2_projects_without_telegram_sources(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    config_file.write_text(
        f"""
runtime:
  state_dir: {state_dir}
projects:
  - slug: ticketdovale
    telegram:
      bot_token_env: ALERT_MONITOR_LEGACY_COMPAT_BOT_TOKEN
      alert_chat_id: "-100111"
    sources:
      - name: ticketdovale-prod-alerts
        type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    sinks:
      - name: ticketdovale-telegram-status
        type: telegram
        bot_token_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN
        chat_id_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID
    hermes:
      coordinator_profile: alert-coordinator
    kanban:
      incident_assignee: debugger
""".strip(),
        encoding="utf-8",
    )

    def fail_get(*args, **kwargs):
        raise AssertionError("Telegram polling should not run for SQS-only v2 projects")

    monkeypatch.setattr("agent_alert_monitor.telegram_ingest.requests.get", fail_get)

    code = main(["--config", str(config_file), "ingest", "--dry-run"], env={})

    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_ingest_surfaces_malformed_sources_for_legacy_projects(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: alpha-api
    telegram:
      alert_chat_id: "-100111"
    sources: not-a-list
    hermes:
      coordinator_profile: alpha-coordinator
    kanban:
      incident_assignee: alpha-debugger
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sources must be a list"):
        main(["--config", str(config_file), "ingest", "--dry-run"], env={})


def test_cli_ingest_surfaces_unknown_source_types(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: alpha-api
    sources:
      - name: typo
        type: telegrm
    hermes:
      coordinator_profile: alpha-coordinator
    kanban:
      incident_assignee: alpha-debugger
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"unsupported projects\[\]\.sources\[\]\.type"):
        main(["--config", str(config_file), "ingest", "--dry-run"], env={})


def test_cli_ingest_expands_env_project_slugs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: ${ALPHA_PROJECT_SLUG}
    telegram:
      bot_token_env: ALERT_MONITOR_ALPHA_BOT_TOKEN
      alert_chat_id: "-100111"
    hermes:
      coordinator_profile: alpha-coordinator
    kanban:
      incident_assignee: alpha-debugger
""".strip(),
        encoding="utf-8",
    )

    calls: list[str] = []

    def fake_get(*args, **kwargs):
        calls.append("getUpdates")

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"ok": True, "result": []}

        return Response()

    monkeypatch.setattr("agent_alert_monitor.telegram_ingest.requests.get", fake_get)

    code = main(
        ["--config", str(config_file), "ingest", "--dry-run"],
        env={"ALPHA_PROJECT_SLUG": "alpha-api", "ALERT_MONITOR_ALPHA_BOT_TOKEN": "token"},
    )

    assert code == 0
    assert calls == ["getUpdates"]


def test_cli_ingest_preserves_unknown_project_errors(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: alpha-api
    telegram:
      alert_chat_id: "-100111"
    hermes:
      coordinator_profile: alpha-coordinator
    kanban:
      incident_assignee: alpha-debugger
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown project slug"):
        main(["--config", str(config_file), "--project", "missing", "ingest", "--dry-run"], env={})


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


def test_all_project_watchdog_does_not_require_env_driven_default_project(
    tmp_path: Path, capsys
) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    config_file.write_text(
        f"""
default_project: ${{DEFAULT_PROJECT}}
runtime:
  state_dir: {state_dir}
projects:
  - slug: sample-api
    telegram:
      alert_chat_id: "-100111"
    hermes:
      coordinator_profile: sample-coordinator
    kanban:
      incident_assignee: sample-debugger
  - slug: worker-queue
    telegram:
      alert_chat_id: "-100222"
    hermes:
      coordinator_profile: worker-coordinator
    kanban:
      incident_assignee: worker-debugger
""".strip(),
        encoding="utf-8",
    )

    code = main(
        ["--config", str(config_file), "watchdog-due"],
        env={"ALERT_MONITOR_STATE_DIR": str(state_dir)},
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


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
