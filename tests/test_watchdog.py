from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_alert_monitor.alert import SqsMessage, parse_alert_text
from agent_alert_monitor.cloud_parsers import AwsSnsCloudWatchAlarmParser
from agent_alert_monitor.ledger import AlertLedger
from agent_alert_monitor.watchdog import WatchdogPolicy, evaluate_stalled_incidents

FIXTURES = Path(__file__).parent / "fixtures"


def test_watchdog_emits_stalled_message_for_silent_open_incident(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    opened = datetime(2026, 6, 13, 12, tzinfo=UTC)
    parsed = parse_alert_text("ALARM: Service5xx service=api")
    ledger.open_incident(
        incident_task_id="t_incident",
        fingerprint="service5xx",
        parsed=parsed,
        status="investigating",
        now=opened,
    )

    due = evaluate_stalled_incidents(
        ledger,
        now=opened + timedelta(minutes=16),
        policy=WatchdogPolicy(stalled_after_seconds=15 * 60),
    )

    assert len(due) == 1
    assert due[0].incident_task_id == "t_incident"
    assert due[0].message.startswith("🚨 Alert monitor stalled")


def test_watchdog_filters_by_incident_scope_when_requested(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    opened = datetime(2026, 6, 13, 12, tzinfo=UTC)
    parsed = parse_alert_text("ALARM: Service5xx service=api")
    alpha_scope = "project:sample-api|profile:alpha-coordinator|board:alpha-board"
    beta_scope = "project:sample-api|profile:beta-coordinator|board:beta-board"
    ledger.open_incident(
        "t_same",
        "sample-api:service5xx",
        parsed,
        "investigating",
        now=opened,
        incident_scope=alpha_scope,
    )
    ledger.open_incident(
        "t_same",
        "sample-api:service5xx",
        parsed,
        "investigating",
        now=opened,
        incident_scope=beta_scope,
    )

    due = evaluate_stalled_incidents(
        ledger,
        now=opened + timedelta(minutes=16),
        policy=WatchdogPolicy(stalled_after_seconds=15 * 60),
        project_slug="sample-api",
        incident_scope=alpha_scope,
    )

    assert len(due) == 1
    assert due[0].incident_scope == alpha_scope


def test_watchdog_includes_stale_sqs_cloud_incident_for_project_scope(
    tmp_path: Path,
) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    opened = datetime(2026, 6, 13, 12, tzinfo=UTC)
    message = SqsMessage(
        message_id="sqs-alarm-1",
        receipt_handle="sanitized-receipt-handle",
        body=(FIXTURES / "aws_sns_cloudwatch_alarm_alarm.json").read_text(encoding="utf-8"),
        raw={"fixture": "alarm"},
    )
    alert = AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale").parse(message)
    opened_result = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", message, alert, now=opened
    )
    assert opened_result.incident_id is not None
    ledger.attach_cloud_incident_kanban_task(opened_result.incident_id, "t_cloud_incident")

    due = evaluate_stalled_incidents(
        ledger,
        now=opened + timedelta(minutes=16),
        policy=WatchdogPolicy(stalled_after_seconds=15 * 60),
        project_slug="ticketdovale",
        incident_scope="project:ticketdovale|profile:alert-coordinator|board:alerts",
    )

    assert len(due) == 1
    assert due[0].incident_task_id == opened_result.incident_id
    assert due[0].incident_scope == "project:ticketdovale|source:sqs"
    assert due[0].message.startswith("🚨 Alert monitor stalled")


def test_watchdog_stays_silent_when_recent_status_exists(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    opened = datetime(2026, 6, 13, 12, tzinfo=UTC)
    parsed = parse_alert_text("ALARM: Service5xx service=api")
    ledger.open_incident("t_incident", "service5xx", parsed, "investigating", now=opened)
    ledger.update_incident_status(
        "t_incident",
        status="investigating",
        last_channel_status="progress",
        now=opened + timedelta(minutes=10),
    )

    due = evaluate_stalled_incidents(
        ledger,
        now=opened + timedelta(minutes=16),
        policy=WatchdogPolicy(stalled_after_seconds=15 * 60),
    )

    assert due == []
