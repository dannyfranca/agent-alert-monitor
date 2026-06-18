from __future__ import annotations

from pathlib import Path


def test_systemd_units_persist_hermes_cli_path_for_live_mode() -> None:
    for unit_path in [
        Path("systemd/agent-alert-monitor-watchdog.service"),
        Path("systemd/agent-alert-monitor-sqs-readiness.service"),
        Path("systemd/agent-alert-monitor-sqs-listen.service"),
        Path("systemd/agent-alert-monitor-health.service"),
    ]:
        text = unit_path.read_text(encoding="utf-8")
        assert "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin" in text


def test_sqs_service_templates_include_readiness_and_live_listener() -> None:
    readiness = Path("systemd/agent-alert-monitor-sqs-readiness.service").read_text(
        encoding="utf-8"
    )
    listener = Path("systemd/agent-alert-monitor-sqs-listen.service").read_text(encoding="utf-8")
    assert " health " in readiness
    assert "sqs-ingest" not in readiness
    assert "sqs-peek" not in readiness
    assert "--dry-run" not in readiness
    assert " sqs-listen " in listener
    assert "sqs-ingest" not in listener
    assert "--dry-run" not in listener


def test_live_mode_docs_include_systemd_hermes_path_smoke_test() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")
    systemd_section = readme.split("## systemd user services", maxsplit=1)[1]
    smoke_test = systemd_section.split("Smoke-test the installed user service", maxsplit=1)[1]
    smoke_test = smoke_test.split("## Common commands", maxsplit=1)[0]

    assert (
        'SYSTEMD_ENV="$(systemctl --user show agent-alert-monitor-health.service '
        '--property=Environment --value)"' in smoke_test
    )
    assert 'printf "%s\\n" "$SYSTEMD_ENV" | tr " " "\\n" | grep "^PATH="' in smoke_test
    assert '--property=Environment="$SYSTEMD_ENV"' in smoke_test
    assert "PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" not in smoke_test
    assert "hermes" in systemd_section
    assert "%h/.local/bin" in env_example


def test_operations_docs_require_sqs_health_and_dry_run_before_live_listen() -> None:
    text = Path("docs/operations.md").read_text(encoding="utf-8")
    non_dry_section = text.split("## Non-dry intake", maxsplit=1)[1]
    non_dry_section = non_dry_section.split("## Watchdog", maxsplit=1)[0]

    assert "agent-alert-monitor --config config.yaml health --source" in non_dry_section
    assert "agent-alert-monitor --config config.yaml sqs-ingest --source" in non_dry_section
    assert "sqs-listen" in non_dry_section
    assert "Telegram status sink" in non_dry_section
    assert "getUpdates" not in non_dry_section
    assert "deleteWebhook" not in non_dry_section


def test_operator_docs_do_not_reference_removed_telegram_intake_paths() -> None:
    docs = [
        Path("README.md"),
        Path("docs/architecture.md"),
        Path("docs/operations.md"),
        Path("docs/kanban-flow.md"),
        Path("config.example.yaml"),
        Path(".env.example"),
    ]
    forbidden = [
        "getUpdates",
        "deleteWebhook",
        "drop_pending_updates",
        "legacy/fallback",
        "legacy fallback",
        "legacy/manual",
        "Telegram fallback",
        "telegram.offset_path",
        "synthetic-alert",
        "agent-alert-monitor ingest",
        "agent-alert-monitor listen",
    ]
    for path in docs:
        text = path.read_text(encoding="utf-8")
        for phrase in forbidden:
            assert phrase not in text, f"{phrase!r} found in {path}"


def test_operations_docs_cover_sqs_health_dlq_retention_and_replay() -> None:
    text = Path("docs/operations.md").read_text(encoding="utf-8")
    sqs_section = text.split("## SQS-first intake operations", maxsplit=1)[1]

    assert "agent-alert-monitor health --source" in sqs_section
    assert "agent-alert-monitor dlq-inspect --source" in sqs_section
    assert "SQS Standard intake queue" in sqs_section
    assert "redrive policy" in sqs_section
    assert "SNS topic" in sqs_section or "EventBridge" in sqs_section
    assert "sqs:GetQueueAttributes" in sqs_section
    assert "sqs:ReceiveMessage" in sqs_section
    assert "sts:GetCallerIdentity" in sqs_section
    assert "14-day" in sqs_section
    assert "EventBridge archive" in sqs_section
    assert "replay" in sqs_section.lower()
    assert "Telegram" in sqs_section and "visible status sink" in sqs_section
