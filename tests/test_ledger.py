from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from agent_alert_monitor.alert import fingerprint_alert, parse_alert_text
from agent_alert_monitor.ledger import AlertLedger


def test_parse_and_fingerprint_normalize_alert_noise() -> None:
    first = parse_alert_text("ALARM: Service5xx service=api region=us-east-1 value=12")
    second = parse_alert_text("ALARM: Service5xx service=api region=us-east-1 value=99")

    assert first.alarm_name == "Service5xx"
    assert first.service == "api"
    assert first.region == "us-east-1"
    assert fingerprint_alert(first) == fingerprint_alert(second)


def test_ingest_is_idempotent_by_platform_chat_and_message(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    now = datetime(2026, 6, 13, 12, tzinfo=UTC)

    first = ledger.ingest_message(
        platform="telegram",
        chat_id="-100123",
        message_id="77",
        raw_text="ALARM: Service5xx service=api region=us-east-1",
        message_ts=now,
    )
    second = ledger.ingest_message(
        platform="telegram",
        chat_id="-100123",
        message_id="77",
        raw_text="ALARM: Service5xx service=api region=us-east-1",
        message_ts=now,
    )

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.message_row_id == first.message_row_id
    assert ledger.count_messages() == 1


def test_related_alerts_correlate_to_existing_open_incident(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    first = ledger.ingest_message(
        platform="telegram",
        chat_id="-100123",
        message_id="1",
        raw_text="ALARM: Service5xx service=api region=us-east-1",
    )
    ledger.open_incident(
        incident_task_id="t_incident",
        fingerprint=first.fingerprint,
        parsed=first.parsed,
        status="investigating",
    )

    second = ledger.ingest_message(
        platform="telegram",
        chat_id="-100123",
        message_id="2",
        raw_text="ALARM: Service5xx service=api region=us-east-1 value=44",
    )

    candidate = ledger.find_open_incident(second.fingerprint)
    assert candidate is not None
    assert candidate.incident_task_id == "t_incident"
    assert second.correlated_incident_task_id == "t_incident"


def test_ledger_runtime_creation_uses_restrictive_permissions(tmp_path: Path) -> None:
    state_dir = tmp_path / "new-state"
    ledger_path = state_dir / "ledger.sqlite"
    previous_umask = os.umask(0)
    try:
        AlertLedger(ledger_path)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600


def test_ledger_does_not_chmod_existing_parent_directory(tmp_path: Path) -> None:
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir(mode=0o755)
    shared_dir.chmod(0o755)

    AlertLedger(shared_dir / "ledger.sqlite")

    assert stat.S_IMODE(shared_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE((shared_dir / "ledger.sqlite").stat().st_mode) == 0o600
