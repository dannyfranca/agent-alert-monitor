from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .config import AgentConfig
from .kanban import KanbanCardRequest, KanbanClient
from .ledger import AlertLedger, Incident

DebuggerClassification = Literal[
    "self-recovered/transient",
    "code-fix-likely",
    "infra-ops-needed",
    "human-decision-needed",
    "missing-access/tooling",
    "false-positive/noise",
]
IncidentLifecycleStatus = Literal[
    "code_fix_queued",
    "awaiting_pr",
    "pr_opened",
    "awaiting_review",
    "ops_blocked",
    "decision_blocked",
    "access_blocked",
    "false_positive",
    "resolved",
]

_ALLOWED_CLASSIFICATIONS: set[str] = {
    "self-recovered/transient",
    "code-fix-likely",
    "infra-ops-needed",
    "human-decision-needed",
    "missing-access/tooling",
    "false-positive/noise",
}


class DebuggerResultValidationError(ValueError):
    """Raised when a debugger_result payload is unsafe to apply to an incident."""


@dataclass(frozen=True)
class DebuggerEvidence:
    source: str
    summary: str
    time_window: str | None = None


@dataclass(frozen=True)
class TelegramStatusEvidence:
    status: str
    evidence: str


@dataclass(frozen=True)
class DebuggerResult:
    incident_id: str
    classification: DebuggerClassification
    confidence: str
    evidence: tuple[DebuggerEvidence, ...]
    suspected_component: str | None
    recommended_next_action: str
    requires_coder: bool
    requires_human: bool
    telegram_status: TelegramStatusEvidence | None


@dataclass(frozen=True)
class LifecycleSyncResult:
    action: str
    incident_id: str
    status: str | None = None
    coder_task_id: str | None = None
    pr_ref: str | None = None


@dataclass(frozen=True)
class PRSuitabilityEvaluation:
    incident_id: str
    pr_ref: str
    suitable: bool
    reasons: tuple[str, ...]


def validate_debugger_result(payload: Mapping[str, object], incident_id: str) -> DebuggerResult:
    if payload.get("type") != "debugger_result":
        raise DebuggerResultValidationError("debugger_result type is required")
    if _required_str(payload, "incident_id") != incident_id:
        raise DebuggerResultValidationError("debugger_result incident_id does not match incident")

    classification = _required_str(payload, "classification")
    if classification not in _ALLOWED_CLASSIFICATIONS:
        raise DebuggerResultValidationError("unsupported debugger_result classification")

    evidence = _parse_evidence(payload.get("evidence"))
    if not evidence:
        raise DebuggerResultValidationError(
            "debugger_result evidence must contain at least one item"
        )

    requires_coder = _required_bool(payload, "requires_coder")
    requires_human = _required_bool(payload, "requires_human")
    telegram_status = _parse_telegram_status(payload.get("telegram_status"))

    if classification == "code-fix-likely" and not requires_coder:
        raise DebuggerResultValidationError(
            "code-fix-likely debugger_result must set requires_coder=true"
        )
    if classification != "code-fix-likely" and requires_coder:
        raise DebuggerResultValidationError(
            "requires_coder=true is only supported for code-fix-likely results"
        )
    if telegram_status is None:
        raise DebuggerResultValidationError(
            "telegram_status evidence is required before closing, blocking, or handing off "
            "an incident"
        )

    return DebuggerResult(
        incident_id=incident_id,
        classification=classification,  # type: ignore[arg-type]
        confidence=_required_str(payload, "confidence"),
        evidence=evidence,
        suspected_component=_optional_str(payload.get("suspected_component")),
        recommended_next_action=_required_str(payload, "recommended_next_action"),
        requires_coder=requires_coder,
        requires_human=requires_human,
        telegram_status=telegram_status,
    )


def sync_debugger_result(
    cfg: AgentConfig,
    *,
    ledger: AlertLedger,
    kanban_client: KanbanClient,
    incident_id: str,
    payload: Mapping[str, object],
    status_sender: Callable[[AgentConfig, str], None],
) -> LifecycleSyncResult:
    incident = _require_incident(ledger, incident_id)
    result = validate_debugger_result(payload, incident_id)
    _require_debugger_syncable_incident(incident, result)

    if result.classification == "self-recovered/transient":
        message = _debugger_status_message(cfg, result, incident)
        status_sender(cfg, message)
        _comment_on_incident(
            ledger,
            kanban_client,
            incident,
            "Debugger classified incident as self-recovered/transient.\n\n" + message,
        )
        ledger.update_incident_status(
            incident.incident_task_id,
            status="resolved",
            last_channel_status="final",
            incident_scope=incident.incident_scope,
        )
        return LifecycleSyncResult("resolved", incident_id, status="resolved")

    if result.classification == "code-fix-likely":
        coder_task_id = _create_coder_card(cfg, kanban_client, incident, result)
        channel_status = None
        if result.telegram_status is not None:
            status_sender(cfg, _debugger_status_message(cfg, result, incident))
            channel_status = "progress"
        ledger.update_incident_status(
            incident.incident_task_id,
            status="code_fix_queued",
            last_channel_status=channel_status,
            coder_task_id=coder_task_id,
            incident_scope=incident.incident_scope,
        )
        _comment_on_incident(
            ledger,
            kanban_client,
            incident,
            (
                f"Debugger classified incident as code-fix-likely; queued coder task "
                f"`{coder_task_id}`.\n\nEvidence:\n{_evidence_markdown(result.evidence)}"
            ),
        )
        return LifecycleSyncResult(
            "coder_queued", incident_id, status="code_fix_queued", coder_task_id=coder_task_id
        )

    status = _status_for_non_coder_classification(result.classification)
    status_sender(cfg, _debugger_status_message(cfg, result, incident))
    _comment_on_incident(
        ledger,
        kanban_client,
        incident,
        (
            f"Debugger classified incident as {result.classification}; "
            f"status `{status}` posted visibly."
        ),
    )
    ledger.update_incident_status(
        incident.incident_task_id,
        status=status,
        last_channel_status="final" if status in {"resolved", "false_positive"} else "progress",
        incident_scope=incident.incident_scope,
    )
    return LifecycleSyncResult("status_updated", incident_id, status=status)


def record_pr_reference(
    ledger: AlertLedger,
    *,
    incident_id: str,
    pr_ref: str,
    status: Literal["pr_opened", "awaiting_review"] = "pr_opened",
) -> LifecycleSyncResult:
    if status not in {"pr_opened", "awaiting_review"}:
        raise ValueError("PR reference status must be pr_opened or awaiting_review")
    incident = _require_incident(ledger, incident_id)
    _require_active_coder_lifecycle(incident)
    cleaned_ref = pr_ref.strip()
    if not cleaned_ref:
        raise ValueError("pr_ref is required")
    ledger.update_incident_status(
        incident.incident_task_id,
        status=status,
        pr_ref=cleaned_ref,
        incident_scope=incident.incident_scope,
    )
    return LifecycleSyncResult("pr_ref_updated", incident_id, status=status, pr_ref=cleaned_ref)


def mark_awaiting_pr(ledger: AlertLedger, *, incident_id: str) -> LifecycleSyncResult:
    incident = _require_incident(ledger, incident_id)
    _require_active_coder_lifecycle(incident)
    ledger.update_incident_status(
        incident.incident_task_id,
        status="awaiting_pr",
        incident_scope=incident.incident_scope,
    )
    return LifecycleSyncResult("awaiting_pr_marked", incident_id, status="awaiting_pr")


def evaluate_pr_suitability(
    *,
    incident_id: str,
    pr_ref: str,
    changed_files: Sequence[str],
    tests: Sequence[str],
    incident_refs: Sequence[str],
    evidence_summary: str,
    suppresses_alarm: bool = False,
    threshold_change: bool = False,
    threshold_evidence: str = "",
    unrelated_changes: Sequence[str] = (),
    touches_secrets_iam_or_deploy: bool = False,
) -> PRSuitabilityEvaluation:
    reasons: list[str] = []
    if suppresses_alarm and not evidence_summary.strip():
        reasons.append("alarm suppression lacks debugger evidence")
    if threshold_change and not threshold_evidence.strip():
        reasons.append("threshold change lacks proof")
    if unrelated_changes:
        reasons.append("unrelated code changed: " + ", ".join(unrelated_changes))
    if not [test for test in tests if test.strip()]:
        reasons.append("missing tests")
    if incident_id not in {ref.strip() for ref in incident_refs}:
        reasons.append("missing incident reference")
    if touches_secrets_iam_or_deploy:
        reasons.append("unexpected secrets/IAM/deploy changes")
    if not [path for path in changed_files if path.strip()]:
        reasons.append("no changed files reported")
    return PRSuitabilityEvaluation(
        incident_id=incident_id,
        pr_ref=pr_ref,
        suitable=not reasons,
        reasons=tuple(reasons),
    )


def record_pr_feedback_if_unsuitable(
    cfg: AgentConfig,
    *,
    ledger: AlertLedger,
    kanban_client: KanbanClient,
    incident_id: str,
    evaluation: PRSuitabilityEvaluation,
    status_sender: Callable[[AgentConfig, str], None],
) -> LifecycleSyncResult:
    if evaluation.incident_id != incident_id:
        raise ValueError("PR suitability evaluation incident_id does not match incident")
    incident = _require_incident(ledger, incident_id)
    _require_active_coder_lifecycle(incident)
    cleaned_ref = evaluation.pr_ref.strip()
    if not cleaned_ref:
        raise ValueError("pr_ref is required")
    if evaluation.suitable:
        return LifecycleSyncResult(
            "pr_suitable", incident_id, status=incident.status, pr_ref=cleaned_ref
        )

    evaluation = PRSuitabilityEvaluation(
        incident_id=evaluation.incident_id,
        pr_ref=cleaned_ref,
        suitable=evaluation.suitable,
        reasons=evaluation.reasons,
    )
    feedback = _unsuitable_pr_message(cfg, incident, evaluation)
    _comment_on_incident(ledger, kanban_client, incident, feedback)
    status_sender(cfg, feedback)
    ledger.update_incident_status(
        incident.incident_task_id,
        status="awaiting_review",
        last_channel_status="progress",
        pr_ref=evaluation.pr_ref,
        incident_scope=incident.incident_scope,
    )
    return LifecycleSyncResult(
        "unsuitable_pr_feedback_posted",
        incident_id,
        status="awaiting_review",
        pr_ref=evaluation.pr_ref,
    )


def _require_incident(ledger: AlertLedger, incident_id: str) -> Incident:
    incident = ledger.get_cloud_incident_by_id(incident_id)
    if incident is None:
        raise ValueError(f"unknown incident_id: {incident_id}")
    return incident


def _require_debugger_syncable_incident(incident: Incident, result: DebuggerResult) -> None:
    if incident.status in {"investigating", "blocked", "stalled"}:
        return
    if incident.status == "self_recovered" and result.classification == "self-recovered/transient":
        return
    raise ValueError(
        "debugger_result updates require an active debugger lifecycle incident"
    )


def _require_active_coder_lifecycle(incident: Incident) -> None:
    if incident.coder_task_id is None or incident.status not in {
        "code_fix_queued",
        "awaiting_pr",
        "pr_opened",
        "awaiting_review",
    }:
        raise ValueError(
            "PR lifecycle updates require an active coder lifecycle with coder_task_id"
        )


def _create_coder_card(
    cfg: AgentConfig,
    kanban_client: KanbanClient,
    incident: Incident,
    result: DebuggerResult,
) -> str:
    assignee = cfg.kanban.coder_assignee or "coder"
    body = f"""# Code Fix Candidate for CloudWatch Incident

Parent incident: {incident.incident_task_id}
Original incident marker: {incident.incident_task_id}
Alarm: {incident.alarm_name or "unknown"}
Service: {incident.service or "unknown"}
Evidence summary:
{_evidence_markdown(result.evidence)}
Suspected code area: {result.suspected_component or "unknown"}

Non-goals:
- Do not change alert thresholds as a first response.
- Do not suppress the alarm unless debugger classified it as false-positive/noise.
- Do not merge without review.

Acceptance:
- Reproduce or explain failure path.
- Add/adjust tests.
- Open PR with canonical incident marker `{incident.incident_task_id}`.
- Post PR ref back to incident card.
"""
    title = (
        f"{cfg.project_display_name} code fix candidate: "
        f"{incident.alarm_name or incident.incident_task_id}"
    )
    return kanban_client.create_incident(
        KanbanCardRequest(
            title=title,
            assignee=assignee,
            body=body,
            priority=cfg.kanban.critical_priority,
            tenant=cfg.kanban.tenant,
            idempotency_key=f"agent-alert-monitor:coder:{incident.incident_task_id}",
        )
    )


def _status_for_non_coder_classification(classification: str) -> IncidentLifecycleStatus:
    if classification == "infra-ops-needed":
        return "ops_blocked"
    if classification == "human-decision-needed":
        return "decision_blocked"
    if classification == "missing-access/tooling":
        return "access_blocked"
    if classification == "false-positive/noise":
        return "false_positive"
    return "awaiting_review"


def _comment_on_incident(
    ledger: AlertLedger,
    kanban_client: KanbanClient,
    incident: Incident,
    body: str,
) -> None:
    task_id = ledger.cloud_incident_kanban_task_id(incident.incident_task_id)
    if task_id:
        kanban_client.comment(task_id, body)


def _debugger_status_message(
    cfg: AgentConfig, result: DebuggerResult, incident: Incident
) -> str:
    if result.telegram_status is None:
        return (
            f"{cfg.project_display_name} alert monitor: "
            f"{result.classification} for {incident.incident_task_id}"
        )
    return (
        f"{cfg.project_display_name} alert monitor\n"
        f"Status: {result.telegram_status.status}\n"
        f"Incident: {incident.incident_task_id}\n"
        f"Evidence: {result.telegram_status.evidence}"
    )


def _unsuitable_pr_message(
    cfg: AgentConfig,
    incident: Incident,
    evaluation: PRSuitabilityEvaluation,
) -> str:
    reasons = "; ".join(evaluation.reasons)
    return (
        f"⚠️ {cfg.project_display_name} alert monitor\n"
        "Status: coder fix not accepted\n"
        f"Incident: {incident.incident_task_id}\n"
        f"PR: {evaluation.pr_ref}\n"
        f"Reason: {reasons}\n"
        "Next: coder/reviewer must revise or operator must approve suppression."
    )


def _evidence_markdown(evidence: Sequence[DebuggerEvidence]) -> str:
    return "\n".join(
        f"- {item.source}: {item.summary}"
        + (f" ({item.time_window})" if item.time_window else "")
        for item in evidence
    )


def _required_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DebuggerResultValidationError(f"debugger_result {key} is required")
    return value.strip()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DebuggerResultValidationError("optional string field must be a string")
    cleaned = value.strip()
    return cleaned or None


def _required_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise DebuggerResultValidationError(f"debugger_result {key} must be true or false")
    return value


def _parse_evidence(value: object) -> tuple[DebuggerEvidence, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise DebuggerResultValidationError("debugger_result evidence must be a list")
    items: list[DebuggerEvidence] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise DebuggerResultValidationError("debugger_result evidence items must be objects")
        source = _required_str(raw, "source")
        summary = _required_str(raw, "summary")
        time_window = _optional_str(raw.get("time_window"))
        items.append(DebuggerEvidence(source=source, summary=summary, time_window=time_window))
    return tuple(items)


def _parse_telegram_status(value: object) -> TelegramStatusEvidence | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DebuggerResultValidationError("debugger_result telegram_status must be an object")
    return TelegramStatusEvidence(
        status=_required_str(value, "status"),
        evidence=_required_str(value, "evidence"),
    )
