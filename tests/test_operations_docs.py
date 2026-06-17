from __future__ import annotations

from pathlib import Path


def test_systemd_units_persist_hermes_cli_path_for_live_mode() -> None:
    for unit_path in [
        Path("systemd/agent-alert-monitor-ingest.service"),
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
    assert " sqs-ingest " in listener
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


def test_operations_docs_require_backlog_drop_or_offset_priming_before_live_listen() -> None:
    text = Path("docs/operations.md").read_text(encoding="utf-8")
    non_dry_section = text.split("## Non-dry intake", maxsplit=1)[1]
    non_dry_section = non_dry_section.split("## Watchdog", maxsplit=1)[0]

    assert "drop_pending_updates" in non_dry_section
    assert "deleteWebhook" in non_dry_section
    assert "prime" in non_dry_section.lower()
    assert "offset" in non_dry_section.lower()
    paragraphs = [paragraph.lower() for paragraph in non_dry_section.split("\n\n")]
    assert any(
        "listen" in paragraph
        and "pending updates" in paragraph
        and "dropped" in paragraph
        and "offset" in paragraph
        and "primed" in paragraph
        for paragraph in paragraphs
    )


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
    assert "Telegram" in sqs_section and "legacy/fallback" in sqs_section
