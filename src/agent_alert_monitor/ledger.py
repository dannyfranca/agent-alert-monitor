from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .alert import ParsedAlert, fingerprint_alert, parse_alert_text


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    dt = dt or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class IngestResult:
    message_row_id: int
    duplicate: bool
    fingerprint: str
    parsed: ParsedAlert
    correlated_incident_task_id: str | None = None


@dataclass(frozen=True)
class Incident:
    incident_task_id: str
    fingerprint: str
    status: str
    severity: str | None
    alarm_name: str | None
    service: str | None
    first_seen_at: str
    last_seen_at: str
    last_channel_post_at: str | None
    last_channel_status: str | None
    coder_task_id: str | None
    pr_ref: str | None


OPEN_STATUSES = {"investigating", "blocked", "stalled"}
CLOSED_STATUSES = {"done", "closed", "resolved"}
INCIDENT_STATUSES = OPEN_STATUSES | CLOSED_STATUSES
CHANNEL_STATUSES = {"acked", "correlated", "progress", "watchdog-stalled", "final"}
STATE_DIR_MODE = 0o700
LEDGER_FILE_MODE = 0o600


class AlertLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=STATE_DIR_MODE)
        if not parent_existed:
            self.path.parent.chmod(STATE_DIR_MODE)
        self._init_db()
        self.path.chmod(LEDGER_FILE_MODE)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alert_messages (
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
                CREATE TABLE IF NOT EXISTS alert_incidents (
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

    def ingest_message(
        self,
        platform: str,
        chat_id: str,
        message_id: str,
        raw_text: str,
        message_ts: datetime | None = None,
        fingerprint_namespace: str | None = None,
    ) -> IngestResult:
        parsed = parse_alert_text(raw_text)
        fingerprint = fingerprint_alert(parsed)
        if fingerprint_namespace:
            fingerprint = f"{fingerprint_namespace}:{fingerprint}"
        normalized_json = json.dumps(asdict(parsed), sort_keys=True)
        correlated = self.find_open_incident(fingerprint)
        incident_task_id = correlated.incident_task_id if correlated else None
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO alert_messages
                    (
                      platform, chat_id, message_id, message_ts, raw_text,
                      normalized_json, fingerprint, incident_task_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform,
                        chat_id,
                        message_id,
                        iso(message_ts) if message_ts else None,
                        raw_text,
                        normalized_json,
                        fingerprint,
                        incident_task_id,
                    ),
                )
                if cur.lastrowid is None:
                    raise RuntimeError("SQLite did not return an alert message row id")
                row_id = int(cur.lastrowid)
                duplicate = False
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT id, incident_task_id FROM alert_messages
                    WHERE platform=? AND chat_id=? AND message_id=?
                    """,
                    (platform, chat_id, message_id),
                ).fetchone()
                row_id = int(row["id"])
                duplicate = True
                incident_task_id = row["incident_task_id"]
        return IngestResult(row_id, duplicate, fingerprint, parsed, incident_task_id)

    def open_incident(
        self,
        incident_task_id: str,
        fingerprint: str,
        parsed: ParsedAlert,
        status: str,
        now: datetime | None = None,
    ) -> Incident:
        _validate_status(status, INCIDENT_STATUSES, "incident status")
        when = iso(now)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_incidents
                (
                  incident_task_id, fingerprint, status, severity,
                  alarm_name, service, first_seen_at, last_seen_at,
                  last_channel_post_at, last_channel_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_task_id) DO UPDATE SET
                  status=excluded.status, last_seen_at=excluded.last_seen_at
                """,
                (
                    incident_task_id,
                    fingerprint,
                    status,
                    parsed.severity,
                    parsed.alarm_name,
                    parsed.service,
                    when,
                    when,
                    None,
                    None,
                ),
            )
            conn.execute(
                """
                UPDATE alert_messages
                SET incident_task_id=?
                WHERE fingerprint=? AND incident_task_id IS NULL
                """,
                (incident_task_id, fingerprint),
            )
        return self.get_incident(incident_task_id)  # type: ignore[return-value]

    def find_open_incident(self, fingerprint: str) -> Incident | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM alert_incidents
                WHERE fingerprint=? AND status NOT IN ('done', 'closed', 'resolved')
                ORDER BY last_seen_at DESC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return _incident_from_row(row) if row else None

    def get_incident(self, incident_task_id: str) -> Incident | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM alert_incidents WHERE incident_task_id=?", (incident_task_id,)
            ).fetchone()
        return _incident_from_row(row) if row else None

    def update_incident_status(
        self,
        incident_task_id: str,
        status: str,
        last_channel_status: str | None = None,
        coder_task_id: str | None = None,
        pr_ref: str | None = None,
        now: datetime | None = None,
    ) -> None:
        _validate_status(status, INCIDENT_STATUSES, "incident status")
        if last_channel_status is not None:
            _validate_status(last_channel_status, CHANNEL_STATUSES, "channel status")
        when = iso(now)
        with self.connect() as conn:
            cur = conn.execute(
                """
                UPDATE alert_incidents
                SET status=?, last_seen_at=?,
                    last_channel_post_at=COALESCE(?, last_channel_post_at),
                    last_channel_status=COALESCE(?, last_channel_status),
                    coder_task_id=COALESCE(?, coder_task_id),
                    pr_ref=COALESCE(?, pr_ref)
                WHERE incident_task_id=?
                """,
                (
                    status,
                    when,
                    when if last_channel_status else None,
                    last_channel_status,
                    coder_task_id,
                    pr_ref,
                    incident_task_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"unknown incident_task_id: {incident_task_id}")

    def idempotency_seed(self, fingerprint: str, fallback_message_id: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT message_id FROM alert_messages
                WHERE fingerprint=? AND incident_task_id IS NULL
                ORDER BY id ASC LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        return str(row["message_id"]) if row else fallback_message_id

    def open_incidents(self) -> list[Incident]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM alert_incidents
                WHERE status NOT IN ('done', 'closed', 'resolved')
                ORDER BY last_seen_at
                """
            ).fetchall()
        return [_incident_from_row(row) for row in rows]

    def count_messages(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM alert_messages").fetchone()[0])


def _incident_from_row(row: sqlite3.Row) -> Incident:
    data: dict[str, Any] = dict(row)
    return Incident(**data)


def _validate_status(value: str, allowed: set[str], label: str) -> None:
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"unsupported {label}: {value!r}; expected one of: {allowed_values}")
