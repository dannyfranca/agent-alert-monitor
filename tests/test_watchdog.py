from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_alert_monitor.alert import parse_alert_text
from agent_alert_monitor.ledger import AlertLedger
from agent_alert_monitor.watchdog import WatchdogPolicy, evaluate_stalled_incidents


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
