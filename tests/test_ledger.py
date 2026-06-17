from __future__ import annotations

import os
import sqlite3
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


def test_same_external_task_id_can_exist_in_different_incident_scopes(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alpha = ledger.ingest_message(
        platform="telegram:alpha-api",
        chat_id="-100111",
        message_id="1",
        raw_text="ALARM: Service5xx service=api",
        fingerprint_namespace="alpha-api",
        incident_scope="project:alpha-api|profile:alpha-coordinator|board:alpha-board",
    )
    beta = ledger.ingest_message(
        platform="telegram:worker-queue",
        chat_id="-100222",
        message_id="2",
        raw_text="ALARM: Service5xx service=worker",
        fingerprint_namespace="worker-queue",
        incident_scope="project:worker-queue|profile:worker-coordinator|board:worker-board",
    )

    ledger.open_incident(
        incident_task_id="t_same",
        fingerprint=alpha.fingerprint,
        parsed=alpha.parsed,
        status="investigating",
        incident_scope="project:alpha-api|profile:alpha-coordinator|board:alpha-board",
    )
    ledger.open_incident(
        incident_task_id="t_same",
        fingerprint=beta.fingerprint,
        parsed=beta.parsed,
        status="investigating",
        incident_scope="project:worker-queue|profile:worker-coordinator|board:worker-board",
    )

    alpha_incident = ledger.get_incident(
        "t_same", incident_scope="project:alpha-api|profile:alpha-coordinator|board:alpha-board"
    )
    beta_scope = "project:worker-queue|profile:worker-coordinator|board:worker-board"
    beta_incident = ledger.get_incident("t_same", incident_scope=beta_scope)

    assert alpha_incident is not None
    assert beta_incident is not None
    assert alpha_incident.service == "api"
    assert beta_incident.service == "worker"
    assert len(ledger.open_incidents()) == 2


def test_message_deduplication_is_scoped_by_incident_route(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alpha_scope = "project:sample-api|profile:alpha-coordinator|board:alpha-board"
    beta_scope = "project:sample-api|profile:beta-coordinator|board:beta-board"

    first = ledger.ingest_message(
        platform="telegram:sample-api",
        chat_id="-100111",
        message_id="same-message",
        raw_text="ALARM: Service5xx service=api",
        fingerprint_namespace="sample-api",
        incident_scope=alpha_scope,
    )
    second = ledger.ingest_message(
        platform="telegram:sample-api",
        chat_id="-100111",
        message_id="same-message",
        raw_text="ALARM: Service5xx service=api",
        fingerprint_namespace="sample-api",
        incident_scope=beta_scope,
    )

    assert first.duplicate is False
    assert second.duplicate is False
    assert ledger.count_messages() == 2


def test_incompatible_telegram_first_incident_schema_is_replaced(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE alert_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              platform TEXT NOT NULL,
              chat_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              message_ts TEXT,
              raw_text TEXT NOT NULL,
              normalized_json TEXT,
              fingerprint TEXT NOT NULL,
              incident_task_id TEXT,
              incident_scope TEXT NOT NULL DEFAULT 'default',
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(incident_scope, platform, chat_id, message_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO alert_messages (
              platform, chat_id, message_id, raw_text, fingerprint,
              incident_task_id, incident_scope
            ) VALUES (
              'telegram:sample-api', '-100111', 'legacy-1', 'ALARM',
              'sample-api:abc123', 't_legacy',
              'project:sample-api|profile:alert-coordinator|board:sample-api-incidents'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE alert_incidents (
              incident_task_id TEXT PRIMARY KEY,
              fingerprint TEXT NOT NULL,
              status TEXT NOT NULL,
              severity TEXT,
              alarm_name TEXT,
              service TEXT,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              last_channel_post_at TEXT,
              last_channel_status TEXT,
              coder_task_id TEXT,
              pr_ref TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO alert_incidents (
              incident_task_id, fingerprint, status, severity, alarm_name, service,
              first_seen_at, last_seen_at
            ) VALUES (
              't_legacy', 'sample-api:abc123', 'investigating', 'critical',
              'Service5xx', 'api', '2026-06-13T12:00:00+00:00',
              '2026-06-13T12:00:00+00:00'
            )
            """
        )

    ledger = AlertLedger(db)
    scope = "project:sample-api|profile:alert-coordinator|board:sample-api-incidents"

    assert ledger.find_open_incident("sample-api:abc123", incident_scope=scope) is None
    with ledger.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(alert_incidents)")}
        row_count = conn.execute("SELECT COUNT(*) FROM alert_incidents").fetchone()[0]
        message = conn.execute("SELECT incident_task_id FROM alert_messages").fetchone()
    duplicate = ledger.ingest_message(
        platform="telegram:sample-api",
        chat_id="-100111",
        message_id="legacy-1",
        raw_text="ALARM: Service5xx service=api",
        fingerprint_namespace="sample-api",
        incident_scope=scope,
    )
    assert {"incident_id", "project_slug", "incident_fingerprint"} <= columns
    assert row_count == 0
    assert message["incident_task_id"] is None
    assert duplicate.duplicate is True
    assert duplicate.correlated_incident_task_id is None


def test_incompatible_message_schema_is_replaced_without_adopting_old_rows(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE alert_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              platform TEXT NOT NULL,
              chat_id TEXT NOT NULL,
              message_id TEXT NOT NULL,
              message_ts TEXT,
              raw_text TEXT NOT NULL,
              normalized_json TEXT,
              fingerprint TEXT NOT NULL,
              incident_task_id TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              UNIQUE(platform, chat_id, message_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO alert_messages (
              platform, chat_id, message_id, raw_text, fingerprint
            ) VALUES ('telegram:sample-api', '-100111', 'legacy-1', 'ALARM', 'sample-api:abc123')
            """
        )

    ledger = AlertLedger(db)
    scope = "project:sample-api|profile:alert-coordinator|board:sample-api-incidents"

    assert ledger.count_messages() == 0
    assert (
        ledger.idempotency_seed("sample-api:abc123", "fallback", incident_scope=scope)
        == "fallback"
    )
    first = ledger.ingest_message(
        platform="telegram:sample-api",
        chat_id="-100111",
        message_id="legacy-1",
        raw_text="ALARM: Service5xx service=api",
        fingerprint_namespace="sample-api",
        incident_scope=scope,
    )
    assert first.duplicate is False


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
