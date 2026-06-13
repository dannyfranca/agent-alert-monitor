from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from .alert import fingerprint_alert, parse_alert_text
from .config import AgentConfig
from .kanban import KanbanCardRequest, KanbanClient
from .ledger import AlertLedger, IngestResult
from .message_templates import correlated_alert_message, intake_ack_message, recovered_message

RECOVERY_STATES = {"OK", "RECOVERY", "RESOLVED"}
CoordinatorAction = Literal[
    "would_create_incident",
    "created_incident",
    "correlated",
    "duplicate",
    "duplicate_closed",
    "recovery_matched",
    "resolved",
    "recovery_unmatched",
]


@dataclass(frozen=True)
class CoordinatorResult:
    action: CoordinatorAction
    incident_task_id: str | None
    channel_message: str
    fingerprint: str
    duplicate: bool
    external_side_effects: bool
    planned_card: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class AlertCoordinator:
    def __init__(
        self,
        config: AgentConfig,
        ledger: AlertLedger | None = None,
        kanban_client: KanbanClient | None = None,
    ):
        self.config = config
        self._ledger = ledger
        self.kanban_client = kanban_client

    @property
    def ledger(self) -> AlertLedger:
        if self._ledger is None:
            self._ledger = AlertLedger(self.config.runtime.ledger_path)
        return self._ledger

    def handle_alert(
        self,
        platform: str,
        chat_id: str,
        message_id: str,
        raw_text: str,
        dry_run: bool = True,
    ) -> CoordinatorResult:
        if dry_run:
            parsed = parse_alert_text(raw_text)
            fingerprint = fingerprint_alert(parsed)
            namespace = self._fingerprint_namespace()
            if namespace:
                fingerprint = f"{namespace}:{fingerprint}"
            request = self._build_card_request(
                IngestResult(0, False, fingerprint, parsed),
                raw_text,
                platform,
                chat_id,
                message_id,
            )
            incident_task_id = f"dryrun-{fingerprint.rsplit(':', 1)[-1][:8]}"
            message = intake_ack_message(
                incident_task_id, parsed.summary, self.config.messages.prefix
            )
            return CoordinatorResult(
                "would_create_incident",
                incident_task_id,
                message,
                fingerprint,
                False,
                False,
                asdict(request),
            )

        ingest = self.ledger.ingest_message(
            self._ledger_platform(platform),
            chat_id,
            message_id,
            raw_text,
            fingerprint_namespace=self._fingerprint_namespace(),
        )
        signal = ingest.parsed.summary
        if ingest.duplicate and ingest.correlated_incident_task_id:
            task_id = ingest.correlated_incident_task_id
            incident = self.ledger.get_incident(task_id)
            if ingest.parsed.state in RECOVERY_STATES:
                action: CoordinatorAction = (
                    "resolved"
                    if incident and incident.status in {"done", "closed", "resolved"}
                    else "recovery_matched"
                )
                message = recovered_message(task_id, signal, self.config.messages.prefix)
                return CoordinatorResult(action, task_id, message, ingest.fingerprint, True, False)
            if incident and incident.status in {"done", "closed", "resolved"}:
                return CoordinatorResult(
                    "duplicate_closed", task_id, "", ingest.fingerprint, True, False
                )
            message = correlated_alert_message(
                task_id or "unknown", signal, self.config.messages.prefix
            )
            return CoordinatorResult("duplicate", task_id, message, ingest.fingerprint, True, False)

        if ingest.correlated_incident_task_id:
            if ingest.parsed.state in RECOVERY_STATES:
                message = recovered_message(
                    ingest.correlated_incident_task_id, signal, self.config.messages.prefix
                )
                return CoordinatorResult(
                    "recovery_matched",
                    ingest.correlated_incident_task_id,
                    message,
                    ingest.fingerprint,
                    False,
                    False,
                )
            message = correlated_alert_message(
                ingest.correlated_incident_task_id, signal, self.config.messages.prefix
            )
            return CoordinatorResult(
                "correlated",
                ingest.correlated_incident_task_id,
                message,
                ingest.fingerprint,
                False,
                False,
            )

        if ingest.parsed.state in RECOVERY_STATES:
            return CoordinatorResult(
                "recovery_unmatched", None, "", ingest.fingerprint, False, False
            )

        idempotency_message_id = self.ledger.idempotency_seed(ingest.fingerprint, message_id)
        request = self._build_card_request(
            ingest, raw_text, platform, chat_id, idempotency_message_id
        )
        if self.kanban_client is None:
            raise RuntimeError("live alert handling requires a Kanban client")
        incident_task_id = self.kanban_client.create_incident(request)
        self.ledger.open_incident(
            incident_task_id, ingest.fingerprint, ingest.parsed, "investigating"
        )
        message = intake_ack_message(
            incident_task_id, signal, self.config.messages.prefix
        )
        return CoordinatorResult(
            "created_incident",
            incident_task_id,
            message,
            ingest.fingerprint,
            ingest.duplicate,
            True,
            None,
        )

    def record_channel_delivery(self, result: CoordinatorResult, last_channel_status: str) -> None:
        if not result.incident_task_id:
            return
        incident = self.ledger.get_incident(result.incident_task_id)
        incident_status = (
            "resolved"
            if result.action in {"recovery_matched", "resolved"}
            else (incident.status if incident else "investigating")
        )
        self.ledger.update_incident_status(
            result.incident_task_id,
            status=incident_status,
            last_channel_status=last_channel_status,
        )

    def _build_card_request(
        self,
        ingest: IngestResult,
        raw_text: str,
        platform: str,
        chat_id: str,
        message_id: str,
    ) -> KanbanCardRequest:
        priority = (
            self.config.kanban.critical_priority
            if ingest.parsed.severity == "critical"
            else self.config.kanban.default_priority
        )
        title = f"{self.config.project_display_name} alert: {ingest.parsed.alarm_name}"
        body = f"""# Application Alert Incident

Project: {self.config.project_display_name} (`{self.config.project_slug}`)

## Alert
- Alarm/service: {ingest.parsed.alarm_name} / {ingest.parsed.service or "unknown"}
- Severity: {ingest.parsed.severity}
- State: {ingest.parsed.state}
- Source: {platform} alert channel {chat_id}/{message_id}
- Fingerprint: {ingest.fingerprint}

## Parsed signal
{ingest.parsed.summary}

## Raw alert text
```text
{raw_text}
```

## Required debugger protocol
1. Post an investigation acknowledgement to the alert channel.
2. Query logs/metrics/recent deploy context for the configured project.
3. Classify: self-recovered, code-fix-likely, infra-ops, human-decision,
   missing-access, false-positive.
4. Post progress if unresolved after the SLA.
5. Never complete/block silently.
6. If code fix is likely, create a high-priority coder card with this incident context.
7. Post final channel status before completing/blocking.

## Alert channel target
`{self.config.hermes.channel_target or "telegram:" + self.config.telegram.alert_chat_id}`
"""
        return KanbanCardRequest(
            title=title,
            assignee=self.config.kanban.incident_assignee,
            body=body,
            priority=priority,
            tenant=self.config.kanban.tenant,
            idempotency_key=f"alert-monitor:{ingest.fingerprint}:{message_id}",
        )

    def _fingerprint_namespace(self) -> str | None:
        if self.config.project_slug == "default":
            return None
        return self.config.project_slug

    def _ledger_platform(self, platform: str) -> str:
        if self.config.project_slug == "default":
            return platform
        return f"{platform}:{self.config.project_slug}"
