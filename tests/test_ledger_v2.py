from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_alert_monitor.alert import ParsedCloudAlert, SqsMessage, parse_alert_text
from agent_alert_monitor.cloud_parsers import AwsSnsCloudWatchAlarmParser
from agent_alert_monitor.ledger import AlertLedger

FIXTURES = Path(__file__).parent / "fixtures"
ACTIVE_STATUSES = {"investigating", "blocked", "stalled", "code_fix_queued", "awaiting_review"}


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def _sqs_message(name: str) -> SqsMessage:
    return SqsMessage(
        message_id=f"sqs-{name}",
        receipt_handle="sanitized-receipt-handle",
        body=_fixture(name),
        raw={"fixture": name},
    )


def _alert(name: str) -> ParsedCloudAlert:
    return AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale").parse(_sqs_message(name))


def _insert_incident_row(conn: sqlite3.Connection, **values: Any) -> None:
    columns = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    conn.execute(
        f"INSERT INTO alert_incidents ({columns}) VALUES ({placeholders})",
        tuple(values.values()),
    )


def test_ledger_v2_schema_is_created_with_idempotency_constraints(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")

    with ledger.connect() as conn:
        tables = {
            row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"alert_sources", "alert_events", "alert_transitions", "alert_incidents"} <= tables

        event_pk = [
            row["name"] for row in conn.execute("PRAGMA table_info(alert_events)") if row["pk"]
        ]
        transition_pk = [
            row["name"] for row in conn.execute("PRAGMA table_info(alert_transitions)") if row["pk"]
        ]
        incident_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(alert_incidents)")
        }
        partial_indexes = {
            row["name"]: {
                "sql": conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (row["name"],)
                ).fetchone()["sql"],
                "unique": bool(row["unique"]),
            }
            for row in conn.execute("PRAGMA index_list(alert_incidents)")
            if row["partial"]
        }

    assert event_pk == ["event_id"]
    assert transition_pk == ["transition_key"]
    assert {
        "incident_id",
        "project_slug",
        "incident_fingerprint",
        "first_event_id",
        "last_event_id",
        "resolved_at",
    } <= incident_columns
    assert any(
        index["unique"]
        and "project_slug" in index["sql"]
        and "incident_fingerprint" in index["sql"]
        and "WHERE status IN" in index["sql"]
        for index in partial_indexes.values()
    )

    with ledger.connect() as conn:
        base = {
            "incident_id": "inc_a",
            "project_slug": "ticketdovale",
            "incident_fingerprint": "cloudwatch-alarm:123:sa-east-1:alarm-a",
            "status": "investigating",
            "first_event_id": "event-a",
            "last_event_id": "event-a",
            "first_seen_at": "2026-06-16T12:00:00+00:00",
            "last_seen_at": "2026-06-16T12:00:00+00:00",
            "incident_scope": "project:ticketdovale|source:sqs",
            "incident_task_id": "inc_a",
            "fingerprint": "cloudwatch-alarm:123:sa-east-1:alarm-a",
        }
        _insert_incident_row(conn, **base)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_incident_row(
                conn,
                **{
                    **base,
                    "incident_id": "inc_b",
                    "first_event_id": "event-b",
                    "last_event_id": "event-b",
                    "incident_task_id": "inc_b",
                },
            )


def test_manual_incidents_use_project_slug_from_scope(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alert = _alert("aws_sns_cloudwatch_alarm_alarm.json")

    parsed = parse_alert_text("ALARM: Service5xx service=payment-processor region=sa-east-1")
    incident = ledger.open_incident(
        incident_task_id="manual_t_1",
        fingerprint=alert.incident_fingerprint,
        parsed=parsed,
        status="investigating",
        incident_scope="project:ticketdovale|source:manual",
    )
    second = ledger.open_incident(
        incident_task_id="manual_t_2",
        fingerprint=alert.incident_fingerprint,
        parsed=parsed,
        status="investigating",
        incident_scope="project:ticketdovale|source:manual",
    )

    assert incident.incident_task_id == "manual_t_1"
    assert second.incident_task_id == "manual_t_2"
    with ledger.connect() as conn:
        rows = conn.execute(
            """
            SELECT incident_id, project_slug, incident_fingerprint
            FROM alert_incidents
            ORDER BY incident_task_id
            """
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["incident_id"].startswith("manual:")
    assert rows[1]["incident_id"].startswith("manual:")
    assert rows[0]["project_slug"].startswith("manual:ticketdovale:")
    assert rows[1]["project_slug"].startswith("manual:ticketdovale:")
    assert rows[0]["project_slug"] != rows[1]["project_slug"]
    assert rows[0]["incident_fingerprint"] == alert.incident_fingerprint
    assert rows[1]["incident_fingerprint"] == alert.incident_fingerprint


def test_record_cloud_event_ignores_duplicate_event_id(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    message = _sqs_message("aws_sns_cloudwatch_alarm_alarm.json")
    alert = _alert("aws_sns_cloudwatch_alarm_alarm.json")

    first = ledger.record_alert_event("ticketdovale-prod-alerts", message, alert)
    second = ledger.record_alert_event("ticketdovale-prod-alerts", message, alert)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.event_id == first.event_id == alert.event_id
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 1
        assert conn.execute("SELECT parse_status FROM alert_events").fetchone()[0] == "parsed"


def test_record_cloud_transition_ignores_duplicate_transition_key(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alert = _alert("aws_sns_cloudwatch_alarm_alarm.json")

    first = ledger.record_alert_transition(alert.event_id, alert)
    duplicate_alert = replace(alert, event_id="sns:arn:aws:sns:sa-east-1:123456789012:topic:other")
    second = ledger.record_alert_transition(duplicate_alert.event_id, duplicate_alert)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.transition_key == first.transition_key == alert.transition_key
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_transitions").fetchone()[0] == 1


def test_alarm_opens_and_correlates_to_single_active_incident(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alarm = _alert("aws_sns_cloudwatch_alarm_alarm.json")
    second_alarm = replace(
        alarm,
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:sns-message-alarm-2",
        state_changed_at="2026-06-16T12:36:56Z",
    )

    opened = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"), alarm
    )
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE alert_incidents SET status='blocked' WHERE incident_id=?",
            (opened.incident_id,),
        )
    correlated = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_ok.json"), second_alarm
    )

    assert opened.action == "opened"
    assert opened.incident_id is not None
    assert correlated.action == "correlated"
    assert correlated.incident_id == opened.incident_id
    with ledger.connect() as conn:
        rows = conn.execute("SELECT * FROM alert_incidents").fetchall()
    assert len(rows) == 1
    assert rows[0]["first_event_id"] == alarm.event_id
    assert rows[0]["last_event_id"] == second_alarm.event_id
    assert rows[0]["status"] == "blocked"


def test_ok_resolves_only_matching_open_incident(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alarm = _alert("aws_sns_cloudwatch_alarm_alarm.json")
    unrelated_alarm = replace(
        alarm,
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:sns-message-other-alarm",
        alarm_name="other-service-prod-lambda-errors-alarm",
        alarm_arn="arn:aws:cloudwatch:sa-east-1:123456789012:alarm:other-service-prod-lambda-errors-alarm",
        state_changed_at="2026-06-16T12:35:56Z",
    )
    other_project_same_alarm = replace(
        alarm,
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:sns-message-other-project",
        project_slug="other-project",
        state_changed_at="2026-06-16T12:36:56Z",
    )
    ok = _alert("aws_sns_cloudwatch_alarm_ok.json")

    target = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"), alarm
    )
    other = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts",
        _sqs_message("aws_sns_cloudwatch_alarm_ok.json"),
        unrelated_alarm,
    )
    other_project = ledger.process_cloud_alert(
        "other-project-alerts",
        _sqs_message("aws_sns_cloudwatch_alarm_ok.json"),
        other_project_same_alarm,
    )
    resolved = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_ok.json"), ok
    )

    assert target.action == "opened"
    assert other.action == "opened"
    assert other_project.action == "opened"
    assert resolved.action == "resolved"
    assert resolved.incident_id == target.incident_id
    with ledger.connect() as conn:
        rows = {
            row["incident_id"]: row
            for row in conn.execute("SELECT incident_id, status, resolved_at FROM alert_incidents")
        }
    assert rows[target.incident_id]["status"] == "self_recovered"
    assert rows[target.incident_id]["resolved_at"] is not None
    assert (
        ledger.find_open_incident(
            ok.incident_fingerprint, incident_scope="project:ticketdovale|source:sqs"
        )
        is None
    )
    assert {incident.incident_task_id for incident in ledger.open_incidents()} == {
        other.incident_id,
        other_project.incident_id,
    }
    assert rows[other.incident_id]["status"] in ACTIVE_STATUSES
    assert rows[other_project.incident_id]["status"] in ACTIVE_STATUSES


def test_stale_ok_does_not_resolve_newer_open_incident(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alarm = replace(
        _alert("aws_sns_cloudwatch_alarm_alarm.json"),
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:newer-alarm",
        state_changed_at="2026-06-16T12:45:56Z",
    )
    later_alarm = replace(
        alarm,
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:latest-alarm",
        state_changed_at="2026-06-16T12:50:56.500+0000",
    )
    stale_alarm = replace(
        alarm,
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:stale-alarm",
        state_changed_at="2026-06-16T12:47:56Z",
    )
    stale_ok = replace(
        _alert("aws_sns_cloudwatch_alarm_ok.json"),
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:older-ok",
        state_changed_at="2026-06-16T12:48:56Z",
    )

    opened = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"), alarm
    )
    correlated = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"), later_alarm
    )
    stale_alarm_result = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"), stale_alarm
    )
    stale = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_ok.json"), stale_ok
    )

    assert opened.action == "opened"
    assert correlated.action == "correlated"
    assert stale_alarm_result.action == "correlated"
    assert stale.action == "stale_ok"
    with ledger.connect() as conn:
        incident = conn.execute(
            "SELECT * FROM alert_incidents WHERE incident_id=?", (opened.incident_id,)
        ).fetchone()
    assert incident["status"] == "investigating"
    assert incident["last_event_id"] == later_alarm.event_id
    assert incident["resolved_at"] is None


def test_ok_older_than_latest_observed_transition_does_not_resolve(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alarm = _alert("aws_sns_cloudwatch_alarm_alarm.json")
    insufficient_data = replace(
        alarm,
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:insufficient-data",
        state="INSUFFICIENT_DATA",
        state_changed_at="2026-06-16T12:50:56.500+0000",
    )
    stale_ok = replace(
        _alert("aws_sns_cloudwatch_alarm_ok.json"),
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:ok-before-insufficient",
        state_changed_at="2026-06-16T12:48:56Z",
    )

    opened = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"), alarm
    )
    observed = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts",
        _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"),
        insufficient_data,
    )
    stale = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_ok.json"), stale_ok
    )

    assert opened.action == "opened"
    assert observed.action == "observed"
    assert stale.action == "stale_ok"
    with ledger.connect() as conn:
        incident = conn.execute(
            "SELECT * FROM alert_incidents WHERE incident_id=?", (opened.incident_id,)
        ).fetchone()
    assert incident["status"] == "investigating"
    assert incident["last_event_id"] == alarm.event_id
    assert incident["resolved_at"] is None


def test_stale_alarm_does_not_reopen_resolved_incident(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alarm = _alert("aws_sns_cloudwatch_alarm_alarm.json")
    ok = _alert("aws_sns_cloudwatch_alarm_ok.json")
    delayed_alarm = replace(
        alarm,
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:delayed-alarm",
        state_changed_at="2026-06-16T12:36:56Z",
    )

    opened = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"), alarm
    )
    resolved = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_ok.json"), ok
    )
    stale_alarm = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts",
        _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"),
        delayed_alarm,
    )

    assert opened.action == "opened"
    assert resolved.action == "resolved"
    assert stale_alarm.action == "stale_alarm"
    with ledger.connect() as conn:
        rows = conn.execute("SELECT * FROM alert_incidents").fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "self_recovered"
    assert rows[0]["last_event_id"] == ok.event_id


def test_alarm_older_than_unmatched_recovery_does_not_open_incident(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    recovery = _alert("aws_sns_cloudwatch_alarm_ok.json")
    delayed_alarm = replace(
        _alert("aws_sns_cloudwatch_alarm_alarm.json"),
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:delayed-after-unmatched-ok",
        state_changed_at="2026-06-16T12:36:56Z",
    )

    unmatched = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", _sqs_message("aws_sns_cloudwatch_alarm_ok.json"), recovery
    )
    stale_alarm = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts",
        _sqs_message("aws_sns_cloudwatch_alarm_alarm.json"),
        delayed_alarm,
    )

    assert unmatched.action == "unmatched_ok"
    assert stale_alarm.action == "stale_alarm"
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_incidents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM alert_transitions").fetchone()[0] == 2


def test_duplicate_event_and_transition_do_not_mutate_incident_state(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    alarm = _alert("aws_sns_cloudwatch_alarm_alarm.json")
    message = _sqs_message("aws_sns_cloudwatch_alarm_alarm.json")

    opened = ledger.process_cloud_alert("ticketdovale-prod-alerts", message, alarm)
    duplicate_event = ledger.process_cloud_alert("ticketdovale-prod-alerts", message, alarm)
    duplicate_transition_alert = replace(
        alarm,
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:sns-message-alarm-redelivery",
    )
    duplicate_transition = ledger.process_cloud_alert(
        "ticketdovale-prod-alerts", message, duplicate_transition_alert
    )

    assert opened.action == "opened"
    assert duplicate_event.action == "duplicate_event"
    assert duplicate_transition.action == "duplicate_transition"
    with ledger.connect() as conn:
        incident = conn.execute("SELECT * FROM alert_incidents").fetchone()
        assert incident["first_event_id"] == alarm.event_id
        assert incident["last_event_id"] == alarm.event_id
        assert conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM alert_transitions").fetchone()[0] == 1
