from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_alert_monitor.alert import parse_alert_text
from agent_alert_monitor.cli import main
from agent_alert_monitor.ledger import AlertLedger


def _sqs_project_yaml(
    *,
    slug: str = "sample-api",
    display_name: str = "Sample API",
    queue_env: str = "SAMPLE_QUEUE_URL",
    chat_id: str = "-100123",
    coordinator: str = "alert-coordinator",
    assignee: str = "debugger",
    tenant: str | None = None,
) -> str:
    return f"""
  - slug: {slug}
    display_name: {display_name}
    sources:
      - name: {slug}-prod-alerts
        type: aws_sqs
        queue_url_env: {queue_env}
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    sinks:
      - name: {slug}-telegram-status
        type: telegram
        bot_token_env: ALERT_MONITOR_{slug.replace('-', '_').upper()}_TELEGRAM_BOT_TOKEN
        chat_id: "{chat_id}"
    hermes:
      coordinator_profile: {coordinator}
      kanban_board: {slug}-incidents
      channel_target: telegram:{chat_id}
    kanban:
      tenant: {tenant or slug}
      incident_assignee: {assignee}
      default_priority: 1000
      critical_priority: 2000
"""


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


def test_cli_manual_alert_dry_run_can_select_project(tmp_path: Path, capsys) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    alpha_project = _sqs_project_yaml(
        slug="alpha-api",
        display_name="Alpha API",
        queue_env="ALPHA_QUEUE_URL",
        chat_id="-100111",
        coordinator="alpha-coordinator",
        assignee="alpha-debugger",
        tenant="alpha",
    )
    beta_project = _sqs_project_yaml(
        slug="beta-worker",
        display_name="Beta Worker",
        queue_env="BETA_QUEUE_URL",
        chat_id="-100222",
        coordinator="beta-coordinator",
        assignee="beta-debugger",
        tenant="beta",
    )
    config_file.write_text(
        f"""
runtime:
  state_dir: {state_dir}
projects:
{alpha_project}
{beta_project}
""".strip(),
        encoding="utf-8",
    )

    code = main(
        [
            "--config",
            str(config_file),
            "--project",
            "beta-worker",
            "manual-alert",
            "--message-id",
            "manual-1",
            "--text",
            "CRITICAL ALARM: QueueDepth service=worker",
            "--dry-run",
        ],
        env={"BETA_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/beta"},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["planned_card"]["assignee"] == "beta-debugger"
    assert payload["planned_card"]["tenant"] == "beta"
    assert "Beta Worker" in payload["planned_card"]["title"]
    assert "manual alert channel beta-worker/manual-1" in payload["planned_card"]["body"]


def test_cli_sqs_ingest_selects_project_by_source(tmp_path: Path, monkeypatch, capsys) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    alpha_project = _sqs_project_yaml(
        slug="alpha-api",
        display_name="Alpha API",
        queue_env="ALPHA_QUEUE_URL",
        chat_id="-100111",
        coordinator="alpha-coordinator",
        assignee="alpha-debugger",
    )
    beta_project = _sqs_project_yaml(
        slug="beta-worker",
        display_name="Beta Worker",
        queue_env="BETA_QUEUE_URL",
        chat_id="-100222",
        coordinator="beta-coordinator",
        assignee="beta-debugger",
    )
    config_file.write_text(
        f"""
runtime:
  state_dir: {state_dir}
projects:
{alpha_project}
{beta_project}
""".strip(),
        encoding="utf-8",
    )
    calls: list[dict[str, object]] = []

    def fake_receive(cfg, **kwargs):
        calls.append({"project": cfg.project_slug, "display": cfg.project_display_name, **kwargs})
        return {"ok": True, "project": cfg.project_slug}

    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.receive_and_parse_sqs_messages", fake_receive
    )

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-ingest",
            "--source",
            "beta-worker-prod-alerts",
            "--dry-run",
        ],
        env={"BETA_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/beta"},
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "project": "beta-worker"}
    assert calls == [
        {
            "project": "beta-worker",
            "display": "Beta Worker",
            "source_name": "beta-worker-prod-alerts",
            "max_messages": None,
            "dry_run": True,
        }
    ]


@pytest.mark.parametrize("command", ["ingest", "listen", "synthetic-alert"])
def test_telegram_first_cli_commands_are_removed(command: str, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main([command, "--text", "ALARM", "--dry-run"])

    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    assert "invalid choice" in stderr
    assert command in stderr


def test_incident_update_rejects_closed_status_without_final_channel_status(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    config_file.write_text(
        f"""
runtime:
  state_dir: {state_dir}
projects:
{_sqs_project_yaml(queue_env="SAMPLE_QUEUE_URL")}
""".strip(),
        encoding="utf-8",
    )
    ledger = AlertLedger(state_dir / "ledger.sqlite")
    parsed = parse_alert_text("ALARM: Service5xx service=api")
    ledger.open_incident(
        "t_incident",
        "service5xx",
        parsed,
        "investigating",
        incident_scope="project:sample-api|profile:alert-coordinator|board:sample-api-incidents",
    )

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
            env={"SAMPLE_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/sample"},
        )

    incident = ledger.get_incident(
        "t_incident",
        incident_scope="project:sample-api|profile:alert-coordinator|board:sample-api-incidents",
    )
    assert incident is not None
    assert incident.status == "investigating"


def test_all_project_watchdog_does_not_require_env_driven_default_project(
    tmp_path: Path, capsys
) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    sample_project = _sqs_project_yaml(
        slug="sample-api",
        queue_env="SAMPLE_QUEUE_URL",
        chat_id="-100111",
        coordinator="sample-coordinator",
        assignee="sample-debugger",
    )
    worker_project = _sqs_project_yaml(
        slug="worker-queue",
        queue_env="WORKER_QUEUE_URL",
        chat_id="-100222",
        coordinator="worker-coordinator",
        assignee="worker-debugger",
    )
    config_file.write_text(
        f"""
default_project: ${{DEFAULT_PROJECT}}
runtime:
  state_dir: {state_dir}
projects:
{sample_project}
{worker_project}
""".strip(),
        encoding="utf-8",
    )

    code = main(
        ["--config", str(config_file), "watchdog-due"],
        env={
            "ALERT_MONITOR_STATE_DIR": str(state_dir),
            "SAMPLE_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/sample",
            "WORKER_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/worker",
        },
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_cli_manual_alert_dry_run_has_no_external_side_effects(tmp_path: Path, capsys) -> None:
    config_file = tmp_path / "config.yaml"
    state_dir = tmp_path / "state"
    config_file.write_text(
        f"""
runtime:
  state_dir: {state_dir}
projects:
{_sqs_project_yaml(queue_env="SAMPLE_QUEUE_URL")}
""".strip(),
        encoding="utf-8",
    )

    code = main(
        [
            "--config",
            str(config_file),
            "manual-alert",
            "--message-id",
            "manual-1",
            "--text",
            "CRITICAL ALARM: Service5xx service=api",
            "--dry-run",
        ],
        env={"SAMPLE_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/sample"},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "would_create_incident"
    assert payload["external_side_effects"] is False
    assert payload["planned_card"]["assignee"] == "debugger"
    assert not state_dir.exists()
