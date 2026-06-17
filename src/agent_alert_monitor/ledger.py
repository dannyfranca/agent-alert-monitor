from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .alert import ParsedAlert, ParsedCloudAlert, SqsMessage, fingerprint_alert, parse_alert_text


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
    correlated_incident_scope: str | None = None


@dataclass(frozen=True)
class Incident:
    incident_task_id: str
    incident_scope: str
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


@dataclass(frozen=True)
class AlertEventWrite:
    event_id: str
    duplicate: bool
    parse_status: str


@dataclass(frozen=True)
class AlertTransitionWrite:
    transition_key: str
    duplicate: bool


@dataclass(frozen=True)
class CloudAlertProcessResult:
    event_id: str
    transition_key: str | None
    action: str
    incident_id: str | None = None
    duplicate_event: bool = False
    duplicate_transition: bool = False


OPEN_STATUSES = {"investigating", "blocked", "stalled"}
CLOSED_STATUSES = {"done", "closed", "resolved"}
V2_ACTIVE_INCIDENT_STATUSES = {
    "investigating",
    "blocked",
    "stalled",
    "code_fix_queued",
    "awaiting_pr",
    "pr_opened",
    "awaiting_review",
    "ops_blocked",
    "decision_blocked",
    "access_blocked",
}
V2_TERMINAL_INCIDENT_STATUSES = {
    "observed",
    "correlated",
    "self_recovered",
    "false_positive",
    "resolved",
    "closed",
}
INCIDENT_STATUSES = (
    OPEN_STATUSES | CLOSED_STATUSES | V2_ACTIVE_INCIDENT_STATUSES | V2_TERMINAL_INCIDENT_STATUSES
)
CHANNEL_STATUSES = {"acked", "correlated", "progress", "watchdog-stalled", "final"}
STATE_DIR_MODE = 0o700
LEDGER_FILE_MODE = 0o600
DEFAULT_INCIDENT_SCOPE = "default"


def _normalize_incident_scope(incident_scope: str | None) -> str:
    return incident_scope or DEFAULT_INCIDENT_SCOPE


def _project_slug_from_scope(incident_scope: str) -> str | None:
    for part in incident_scope.split("|"):
        if part.startswith("project:"):
            return part.removeprefix("project:")
    return None



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
            self._ensure_messages_schema(conn)
            self._ensure_sources_schema(conn)
            self._ensure_events_schema(conn)
            self._ensure_transitions_schema(conn)
            self._ensure_incidents_schema(conn)

    def _ensure_sources_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_sources (
              source_name TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              project_slug TEXT NOT NULL,
              queue_url TEXT,
              envelope TEXT,
              created_at TEXT NOT NULL
            )
            """
        )

    def _ensure_events_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_events (
              event_id TEXT PRIMARY KEY,
              source_name TEXT NOT NULL,
              project_slug TEXT NOT NULL,
              received_at TEXT NOT NULL,
              raw_sqs_message_json TEXT NOT NULL,
              raw_envelope_json TEXT,
              normalized_alert_json TEXT,
              parse_status TEXT NOT NULL,
              parse_error TEXT
            )
            """
        )

    def _ensure_transitions_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_transitions (
              transition_key TEXT PRIMARY KEY,
              event_id TEXT NOT NULL,
              incident_fingerprint TEXT NOT NULL,
              state TEXT NOT NULL,
              previous_state TEXT,
              state_changed_at TEXT NOT NULL,
              alarm_arn TEXT,
              alarm_name TEXT,
              service TEXT,
              log_group TEXT,
              created_at TEXT NOT NULL
            )
            """
        )

    def _ensure_messages_schema(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(alert_messages)").fetchall()
        if not rows:
            self._create_messages_table(conn)
            return
        columns = {row["name"] for row in rows}
        has_scoped_unique = False
        for index in conn.execute("PRAGMA index_list(alert_messages)").fetchall():
            if not index["unique"]:
                continue
            index_columns = [
                row["name"] for row in conn.execute(f"PRAGMA index_info({index['name']})")
            ]
            if index_columns == ["incident_scope", "platform", "chat_id", "message_id"]:
                has_scoped_unique = True
                break
        if "incident_scope" in columns and has_scoped_unique:
            conn.execute(
                "UPDATE alert_messages SET incident_scope=? WHERE incident_scope IS NULL",
                (DEFAULT_INCIDENT_SCOPE,),
            )
            return

        conn.execute("DROP TABLE alert_messages")
        self._create_messages_table(conn)

    def _create_messages_table(self, conn: sqlite3.Connection) -> None:
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

    def _ensure_incidents_schema(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(alert_incidents)").fetchall()
        if not rows:
            self._create_incidents_table(conn)
            return
        columns = {row["name"] for row in rows}
        if {"incident_id", "project_slug", "incident_fingerprint"} <= columns:
            self._ensure_open_incident_index(conn)
            return

        conn.execute("DROP TABLE alert_incidents")
        self._create_incidents_table(conn)
        conn.execute("UPDATE alert_messages SET incident_task_id=NULL")

    def _create_incidents_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE alert_incidents (
              incident_id TEXT PRIMARY KEY,
              project_slug TEXT NOT NULL,
              incident_fingerprint TEXT NOT NULL,
              status TEXT NOT NULL,
              severity TEXT,
              alarm_arn TEXT,
              alarm_name TEXT,
              service TEXT,
              log_group TEXT,
              first_event_id TEXT NOT NULL,
              last_event_id TEXT NOT NULL,
              kanban_task_id TEXT,
              coder_task_id TEXT,
              pr_ref TEXT,
              first_seen_at TEXT NOT NULL,
              last_seen_at TEXT NOT NULL,
              resolved_at TEXT,
              last_channel_post_at TEXT,
              last_channel_status TEXT,
              incident_scope TEXT NOT NULL DEFAULT 'default',
              incident_task_id TEXT NOT NULL,
              fingerprint TEXT NOT NULL,
              UNIQUE(incident_scope, incident_task_id)
            )
            """
        )
        self._ensure_open_incident_index(conn)

    def _ensure_open_incident_index(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_open_incident_by_fingerprint
            ON alert_incidents(project_slug, incident_fingerprint)
            WHERE status IN (
              'access_blocked', 'awaiting_pr', 'awaiting_review', 'blocked', 'code_fix_queued',
                    'decision_blocked', 'investigating', 'ops_blocked', 'pr_opened', 'stalled'
            )
            """
        )

    def record_alert_event(
        self,
        source_name: str,
        raw_sqs_message: SqsMessage,
        alert: ParsedCloudAlert,
        *,
        received_at: datetime | None = None,
    ) -> AlertEventWrite:
        with self.connect() as conn:
            return self._record_alert_event(conn, source_name, raw_sqs_message, alert, received_at)

    def _record_alert_event(
        self,
        conn: sqlite3.Connection,
        source_name: str,
        raw_sqs_message: SqsMessage,
        alert: ParsedCloudAlert,
        received_at: datetime | None,
    ) -> AlertEventWrite:
        when = iso(received_at)
        conn.execute(
            """
            INSERT INTO alert_sources (
              source_name, source_type, project_slug, queue_url, envelope, created_at
            ) VALUES (?, ?, ?, NULL, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
              source_type=excluded.source_type,
              project_slug=excluded.project_slug,
              envelope=excluded.envelope
            """,
            (source_name, alert.source_type, alert.project_slug, alert.source_type, when),
        )
        try:
            conn.execute(
                """
                INSERT INTO alert_events (
                  event_id, source_name, project_slug, received_at, raw_sqs_message_json,
                  raw_envelope_json, normalized_alert_json, parse_status, parse_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', NULL)
                """,
                (
                    alert.event_id,
                    source_name,
                    alert.project_slug,
                    when,
                    _json_dumps(_sqs_message_payload(raw_sqs_message)),
                    _json_dumps(alert.raw),
                    _json_dumps(asdict(alert)),
                ),
            )
        except sqlite3.IntegrityError:
            return AlertEventWrite(alert.event_id, True, "parsed")
        return AlertEventWrite(alert.event_id, False, "parsed")

    def record_alert_transition(
        self,
        event_id: str,
        alert: ParsedCloudAlert,
        *,
        created_at: datetime | None = None,
    ) -> AlertTransitionWrite:
        with self.connect() as conn:
            return self._record_alert_transition(conn, event_id, alert, created_at)

    def _record_alert_transition(
        self,
        conn: sqlite3.Connection,
        event_id: str,
        alert: ParsedCloudAlert,
        created_at: datetime | None,
    ) -> AlertTransitionWrite:
        try:
            conn.execute(
                """
                INSERT INTO alert_transitions (
                  transition_key, event_id, incident_fingerprint, state, previous_state,
                  state_changed_at, alarm_arn, alarm_name, service, log_group, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert.transition_key,
                    event_id,
                    alert.incident_fingerprint,
                    alert.state,
                    alert.previous_state,
                    alert.state_changed_at,
                    alert.alarm_arn,
                    alert.alarm_name,
                    _optional_json_text(alert.metadata.get("service")),
                    _optional_json_text(alert.metadata.get("log_group")),
                    iso(created_at),
                ),
            )
        except sqlite3.IntegrityError:
            return AlertTransitionWrite(alert.transition_key, True)
        return AlertTransitionWrite(alert.transition_key, False)

    def process_cloud_alert(
        self,
        source_name: str,
        raw_sqs_message: SqsMessage,
        alert: ParsedCloudAlert,
        *,
        now: datetime | None = None,
    ) -> CloudAlertProcessResult:
        with self.connect() as conn:
            event = self._record_alert_event(conn, source_name, raw_sqs_message, alert, now)
            if event.duplicate:
                return CloudAlertProcessResult(
                    alert.event_id,
                    alert.transition_key,
                    "duplicate_event",
                    duplicate_event=True,
                )
            transition = self._record_alert_transition(conn, alert.event_id, alert, now)
            if transition.duplicate:
                return CloudAlertProcessResult(
                    alert.event_id,
                    alert.transition_key,
                    "duplicate_transition",
                    duplicate_transition=True,
                )
            action, incident_id = self._apply_cloud_incident_transition(conn, alert, now)
            return CloudAlertProcessResult(
                alert.event_id, alert.transition_key, action, incident_id
            )

    def _apply_cloud_incident_transition(
        self,
        conn: sqlite3.Connection,
        alert: ParsedCloudAlert,
        now: datetime | None,
    ) -> tuple[str, str | None]:
        if alert.state == "ALARM":
            existing = _find_open_cloud_incident_row(
                conn, alert.project_slug, alert.incident_fingerprint
            )
            if existing is not None:
                if _alert_is_older_than_latest_project_transition(conn, alert):
                    return "correlated", str(existing["incident_id"])
                when = iso(now)
                conn.execute(
                    """
                    UPDATE alert_incidents
                    SET last_event_id=?, last_seen_at=?
                    WHERE incident_id=?
                    """,
                    (alert.event_id, when, existing["incident_id"]),
                )
                return "correlated", str(existing["incident_id"])
            if _alert_is_older_than_latest_project_transition(conn, alert):
                return "stale_alarm", None
            incident_id = _cloud_incident_id(alert)
            incident_scope = f"project:{alert.project_slug}|source:sqs"
            when = iso(now)
            conn.execute(
                """
                INSERT INTO alert_incidents (
                  incident_id, project_slug, incident_fingerprint, status, severity,
                  alarm_arn, alarm_name, service, log_group, first_event_id,
                  last_event_id, first_seen_at, last_seen_at, incident_scope,
                  incident_task_id, fingerprint
                ) VALUES (?, ?, ?, 'investigating', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident_id,
                    alert.project_slug,
                    alert.incident_fingerprint,
                    _severity_for_cloud_alert(alert),
                    alert.alarm_arn,
                    alert.alarm_name,
                    _optional_json_text(alert.metadata.get("service")),
                    _optional_json_text(alert.metadata.get("log_group")),
                    alert.event_id,
                    alert.event_id,
                    when,
                    when,
                    incident_scope,
                    incident_id,
                    alert.incident_fingerprint,
                ),
            )
            return "opened", incident_id

        if alert.state in {"OK", "RECOVERY", "RESOLVED"}:
            existing = _find_open_cloud_incident_row(
                conn, alert.project_slug, alert.incident_fingerprint
            )
            if existing is None:
                return "unmatched_ok", None
            if _alert_is_older_than_latest_project_transition(conn, alert):
                return "stale_ok", None
            when = iso(now)
            conn.execute(
                """
                UPDATE alert_incidents
                SET status='self_recovered', last_event_id=?, last_seen_at=?, resolved_at=?
                WHERE incident_id=?
                """,
                (alert.event_id, when, when, existing["incident_id"]),
            )
            return "resolved", str(existing["incident_id"])

        return "observed", None

    def ingest_message(
        self,
        platform: str,
        chat_id: str,
        message_id: str,
        raw_text: str,
        message_ts: datetime | None = None,
        fingerprint_namespace: str | None = None,
        incident_scope: str | None = None,
    ) -> IngestResult:
        incident_scope = _normalize_incident_scope(incident_scope)
        parsed = parse_alert_text(raw_text)
        fingerprint = fingerprint_alert(parsed)
        if fingerprint_namespace:
            fingerprint = f"{fingerprint_namespace}:{fingerprint}"
        normalized_json = json.dumps(asdict(parsed), sort_keys=True)
        correlated = self.find_open_incident(fingerprint, incident_scope=incident_scope)
        incident_task_id = correlated.incident_task_id if correlated else None
        correlated_scope = correlated.incident_scope if correlated else None
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO alert_messages
                    (
                      platform, chat_id, message_id, message_ts, raw_text,
                      normalized_json, fingerprint, incident_task_id, incident_scope
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        incident_scope,
                    ),
                )
                if cur.lastrowid is None:
                    raise RuntimeError("SQLite did not return an alert message row id")
                row_id = int(cur.lastrowid)
                duplicate = False
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT id, incident_task_id, incident_scope FROM alert_messages
                    WHERE incident_scope=? AND platform=? AND chat_id=? AND message_id=?
                    """,
                    (incident_scope, platform, chat_id, message_id),
                ).fetchone()
                row_id = int(row["id"])
                duplicate = True
                incident_task_id = row["incident_task_id"]
                correlated_scope = row["incident_scope"]
        return IngestResult(
            row_id, duplicate, fingerprint, parsed, incident_task_id, correlated_scope
        )

    def open_incident(
        self,
        incident_task_id: str,
        fingerprint: str,
        parsed: ParsedAlert,
        status: str,
        now: datetime | None = None,
        incident_scope: str | None = None,
    ) -> Incident:
        _validate_status(status, INCIDENT_STATUSES, "incident status")
        incident_scope = _normalize_incident_scope(incident_scope)
        when = iso(now)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO alert_incidents
                (
                  incident_id, project_slug, incident_fingerprint, status, severity,
                  alarm_name, service, first_event_id, last_event_id, first_seen_at,
                  last_seen_at, last_channel_post_at, last_channel_status,
                  incident_scope, incident_task_id, fingerprint
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_scope, incident_task_id) DO UPDATE SET
                  status=excluded.status, last_seen_at=excluded.last_seen_at
                """,
                (
                    f"manual:{_stable_digest(f'{incident_scope}:{incident_task_id}')}",
                    _manual_project_slug(incident_scope, fingerprint, incident_task_id),
                    fingerprint,
                    status,
                    parsed.severity,
                    parsed.alarm_name,
                    parsed.service,
                    f"manual:{incident_scope}:{incident_task_id}:first",
                    f"manual:{incident_scope}:{incident_task_id}:last",
                    when,
                    when,
                    None,
                    None,
                    incident_scope,
                    incident_task_id,
                    fingerprint,
                ),
            )
            conn.execute(
                """
                UPDATE OR IGNORE alert_messages
                SET incident_task_id=?, incident_scope=?
                WHERE fingerprint=? AND incident_task_id IS NULL
                  AND (incident_scope=? OR (? != ? AND incident_scope=?))
                """,
                (
                    incident_task_id,
                    incident_scope,
                    fingerprint,
                    incident_scope,
                    incident_scope,
                    DEFAULT_INCIDENT_SCOPE,
                    DEFAULT_INCIDENT_SCOPE,
                ),
            )
        return self.get_incident(incident_task_id, incident_scope=incident_scope)  # type: ignore[return-value]

    def find_open_incident(
        self, fingerprint: str, incident_scope: str | None = None
    ) -> Incident | None:
        params: tuple[str, ...]
        scope_clause = ""
        if incident_scope is not None:
            scope_clause = "AND incident_scope=?"
            params = (fingerprint, _normalize_incident_scope(incident_scope))
        else:
            params = (fingerprint,)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM alert_incidents
                WHERE fingerprint=? {scope_clause}
                  AND status IN (
                    'access_blocked', 'awaiting_pr', 'awaiting_review', 'blocked',
                    'code_fix_queued', 'decision_blocked', 'investigating',
                    'ops_blocked', 'pr_opened', 'stalled'
                  )
                ORDER BY last_seen_at DESC LIMIT 1
                """,
                params,
            ).fetchone()
        return _incident_from_row(row) if row else None

    def get_incident(
        self, incident_task_id: str, incident_scope: str | None = DEFAULT_INCIDENT_SCOPE
    ) -> Incident | None:
        params: tuple[str, ...]
        scope_clause = ""
        if incident_scope is not None:
            scope_clause = "AND incident_scope=?"
            params = (incident_task_id, _normalize_incident_scope(incident_scope))
        else:
            params = (incident_task_id,)
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT * FROM alert_incidents WHERE incident_task_id=? {scope_clause}",
                params,
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
        incident_scope: str | None = DEFAULT_INCIDENT_SCOPE,
    ) -> None:
        _validate_status(status, INCIDENT_STATUSES, "incident status")
        if last_channel_status is not None:
            _validate_status(last_channel_status, CHANNEL_STATUSES, "channel status")
        incident_scope = _normalize_incident_scope(incident_scope)
        when = iso(now)
        with self.connect() as conn:
            current = conn.execute(
                """
                SELECT last_channel_status FROM alert_incidents
                WHERE incident_scope=? AND incident_task_id=?
                """,
                (incident_scope, incident_task_id),
            ).fetchone()
            if current is None:
                raise ValueError(f"unknown incident_task_id: {incident_task_id}")
            effective_channel_status = last_channel_status or current["last_channel_status"]
            if status in CLOSED_STATUSES and effective_channel_status != "final":
                raise ValueError("closed incident statuses require final channel status evidence")
            cur = conn.execute(
                """
                UPDATE alert_incidents
                SET status=?, last_seen_at=?,
                    last_channel_post_at=COALESCE(?, last_channel_post_at),
                    last_channel_status=COALESCE(?, last_channel_status),
                    coder_task_id=COALESCE(?, coder_task_id),
                    pr_ref=COALESCE(?, pr_ref)
                WHERE incident_scope=? AND incident_task_id=?
                """,
                (
                    status,
                    when,
                    when if last_channel_status else None,
                    last_channel_status,
                    coder_task_id,
                    pr_ref,
                    incident_scope,
                    incident_task_id,
                ),
            )
            if cur.rowcount == 0:
                raise ValueError(f"unknown incident_task_id: {incident_task_id}")

    def idempotency_seed(
        self,
        fingerprint: str,
        fallback_message_id: str,
        incident_scope: str | None = DEFAULT_INCIDENT_SCOPE,
    ) -> str:
        incident_scope = _normalize_incident_scope(incident_scope)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT id, message_id FROM alert_messages
                WHERE fingerprint=? AND incident_scope=? AND incident_task_id IS NULL
                ORDER BY id ASC LIMIT 1
                """,
                (fingerprint, incident_scope),
            ).fetchone()
        return str(row["message_id"]) if row else fallback_message_id

    def open_incidents(self) -> list[Incident]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM alert_incidents
                WHERE status IN (
                  'access_blocked', 'awaiting_pr', 'awaiting_review', 'blocked', 'code_fix_queued',
                    'decision_blocked', 'investigating', 'ops_blocked', 'pr_opened', 'stalled'
                )
                ORDER BY last_seen_at
                """
            ).fetchall()
        return [_incident_from_row(row) for row in rows]

    def count_messages(self) -> int:
        with self.connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM alert_messages").fetchone()[0])


def _incident_from_row(row: sqlite3.Row) -> Incident:
    row_data: dict[str, Any] = dict(row)
    incident_fields = {field.name for field in fields(Incident)}
    data = {key: value for key, value in row_data.items() if key in incident_fields}
    return Incident(**data)


def _validate_status(value: str, allowed: set[str], label: str) -> None:
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"unsupported {label}: {value!r}; expected one of: {allowed_values}")


def _stable_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _project_slug_from_fingerprint(fingerprint: str) -> str:
    if ":" in fingerprint:
        return fingerprint.split(":", 1)[0]
    return "default"


def _manual_project_slug(incident_scope: str, fingerprint: str, incident_task_id: str) -> str:
    project_slug = _project_slug_from_scope(incident_scope) or _project_slug_from_fingerprint(
        fingerprint
    )
    seed = f"{incident_scope}:{incident_task_id}"
    return f"manual:{project_slug}:{_stable_digest(seed)}"


def _json_dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sqs_message_payload(message: SqsMessage) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "receipt_handle": message.receipt_handle,
        "body": message.body,
        "attributes": message.attributes,
        "message_attributes": message.message_attributes,
        "raw": message.raw,
    }


def _optional_json_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _severity_for_cloud_alert(alert: ParsedCloudAlert) -> str | None:
    if alert.state == "ALARM":
        return "critical"
    return None


def _cloud_incident_id(alert: ParsedCloudAlert) -> str:
    seed = f"{alert.project_slug}:{alert.incident_fingerprint}:{alert.event_id}"
    return f"inc_{_stable_digest(seed)}"


def _parse_cloud_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _alert_is_older_than_latest_project_transition(
    conn: sqlite3.Connection, alert: ParsedCloudAlert
) -> bool:
    rows = conn.execute(
        """
        SELECT t.state_changed_at
        FROM alert_transitions t
        JOIN alert_events e ON e.event_id = t.event_id
        WHERE e.project_slug=? AND t.incident_fingerprint=?
        """,
        (alert.project_slug, alert.incident_fingerprint),
    ).fetchall()
    if not rows:
        return False
    latest_seen = max(_parse_cloud_timestamp(str(row["state_changed_at"])) for row in rows)
    return _parse_cloud_timestamp(alert.state_changed_at) < latest_seen


def _find_open_cloud_incident_row(
    conn: sqlite3.Connection, project_slug: str, incident_fingerprint: str
) -> sqlite3.Row | None:
    placeholders = ", ".join("?" for _ in V2_ACTIVE_INCIDENT_STATUSES)
    params = (project_slug, incident_fingerprint, *sorted(V2_ACTIVE_INCIDENT_STATUSES))
    return conn.execute(
        f"""
        SELECT * FROM alert_incidents
        WHERE project_slug=? AND incident_fingerprint=? AND status IN ({placeholders})
        ORDER BY last_seen_at DESC LIMIT 1
        """,
        params,
    ).fetchone()
