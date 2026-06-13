from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .ledger import AlertLedger, Incident
from .message_templates import stalled_message


@dataclass(frozen=True)
class WatchdogPolicy:
    ack_sla_seconds: int = 120
    progress_sla_seconds: int = 600
    stalled_after_seconds: int = 900


@dataclass(frozen=True)
class WatchdogFinding:
    incident_task_id: str
    incident_scope: str
    message: str
    age_seconds: int


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _last_visible_status_at(incident: Incident) -> datetime:
    return _parse_iso(incident.last_channel_post_at or incident.first_seen_at)


def _threshold_seconds(incident: Incident, policy: WatchdogPolicy) -> int:
    if incident.last_channel_post_at is None:
        return policy.ack_sla_seconds
    if incident.last_channel_status in {"acked", "correlated", "progress"}:
        return policy.progress_sla_seconds
    return policy.stalled_after_seconds


def _belongs_to_project(incident: Incident, project_slug: str | None) -> bool:
    if project_slug is None:
        return True
    if project_slug == "default":
        return ":" not in incident.fingerprint
    return incident.fingerprint.startswith(f"{project_slug}:")


def _belongs_to_scope(incident: Incident, incident_scope: str | None) -> bool:
    if incident_scope is None:
        return True
    if incident.incident_scope == incident_scope:
        return True
    return incident.incident_scope == "default" and incident_scope != "default"


def evaluate_stalled_incidents(
    ledger: AlertLedger,
    now: datetime | None = None,
    policy: WatchdogPolicy | None = None,
    project_slug: str | None = None,
    incident_scope: str | None = None,
    message_prefix: str = "Alert monitor",
) -> list[WatchdogFinding]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    policy = policy or WatchdogPolicy()
    findings: list[WatchdogFinding] = []
    for incident in ledger.open_incidents():
        if not _belongs_to_project(incident, project_slug):
            continue
        if not _belongs_to_scope(incident, incident_scope):
            continue
        last_at = _last_visible_status_at(incident)
        age = int((now - last_at).total_seconds())
        if age > _threshold_seconds(incident, policy):
            minutes = max(1, age // 60)
            last_status = incident.last_channel_status or incident.status
            findings.append(
                WatchdogFinding(
                    incident_task_id=incident.incident_task_id,
                    incident_scope=incident.incident_scope,
                    message=stalled_message(
                        incident.incident_task_id,
                        f"{minutes} minutes ago / {last_status}",
                        message_prefix,
                    ),
                    age_seconds=age,
                )
            )
    return findings
