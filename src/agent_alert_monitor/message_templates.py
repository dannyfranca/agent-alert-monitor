from __future__ import annotations

MAX_FIELD = 160


def _clip(value: str, limit: int = MAX_FIELD) -> str:
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def intake_ack_message(
    incident_task_id: str, signal: str, prefix: str = "Alert monitor"
) -> str:
    return "\n".join(
        [
            f"🔎 {prefix}",
            "Status: investigating",
            f"Incident: {incident_task_id}",
            f"Signal: {_clip(signal)}",
            "Next: debugger is checking logs/metrics.",
        ]
    )


def correlated_alert_message(
    incident_task_id: str, signal: str, prefix: str = "Alert monitor"
) -> str:
    return "\n".join(
        [
            f"🔁 {prefix}",
            "Status: correlated with existing incident",
            f"Incident: {incident_task_id}",
            f"Signal: {_clip(signal)}",
            "Next: debugger context updated.",
        ]
    )


def progress_message(
    incident_task_id: str,
    evidence: str,
    next_step: str,
    prefix: str = "Alert monitor",
) -> str:
    return "\n".join(
        [
            f"⏳ {prefix}",
            "Status: still investigating",
            f"Incident: {incident_task_id}",
            f"Evidence: {_clip(evidence)}",
            f"Next: {_clip(next_step)}",
        ]
    )


def recovered_message(
    incident_task_id: str, evidence: str, prefix: str = "Alert monitor"
) -> str:
    return "\n".join(
        [
            f"✅ {prefix} resolved",
            "Status: self-recovered / transient",
            f"Incident: {incident_task_id}",
            f"Evidence: {_clip(evidence)}",
            "Next: no code action queued.",
        ]
    )


def coder_queued_message(
    incident_task_id: str,
    coder_task_id: str,
    evidence: str,
    prefix: str = "Alert monitor",
) -> str:
    return "\n".join(
        [
            f"🛠️ {prefix}",
            "Status: code fix likely; coder queued",
            f"Incident: {incident_task_id}",
            f"Coder card: {coder_task_id}",
            f"Evidence: {_clip(evidence)}",
            "Next: PR will be posted here when opened.",
        ]
    )


def pr_opened_message(
    incident_task_id: str,
    coder_task_id: str,
    owner_repo: str,
    number: int,
    prefix: str = "Alert monitor",
) -> str:
    return "\n".join(
        [
            f"📌 {prefix}",
            "Status: PR opened",
            f"Incident: {incident_task_id}",
            f"Coder card: {coder_task_id}",
            f"PR: github:{owner_repo}/pull/{number}",
            "Next: awaiting CI/review.",
        ]
    )


def ops_blocked_message(
    incident_task_id: str, need: str, why: str, prefix: str = "Alert monitor"
) -> str:
    return "\n".join(
        [
            f"⚠️ {prefix} needs ops action",
            "Status: blocked",
            f"Incident: {incident_task_id}",
            f"Need: {_clip(need)}",
            f"Why: {_clip(why)}",
            "Next: unblock after action is done.",
        ]
    )


def decision_needed_message(
    incident_task_id: str,
    decision: str,
    options: str,
    recommendation: str,
    prefix: str = "Alert monitor",
) -> str:
    return "\n".join(
        [
            f"❓ {prefix} needs decision",
            "Status: blocked",
            f"Incident: {incident_task_id}",
            f"Decision: {_clip(decision)}",
            f"Options: {_clip(options)}",
            f"Recommendation: {_clip(recommendation)}",
        ]
    )


def missing_access_message(
    incident_task_id: str, missing: str, prefix: str = "Alert monitor"
) -> str:
    return "\n".join(
        [
            f"🔐 {prefix} blocked",
            "Status: missing access/tooling",
            f"Incident: {incident_task_id}",
            f"Missing: {_clip(missing)}",
            "Next: fix prerequisite, then unblock incident.",
        ]
    )


def stalled_message(
    incident_task_id: str, last_seen: str, prefix: str = "Alert monitor"
) -> str:
    return "\n".join(
        [
            f"🚨 {prefix} stalled",
            "Status: no update within SLA",
            f"Incident: {incident_task_id}",
            f"Last seen: {_clip(last_seen)}",
            "Next: watchdog will reclaim/escalate unless progress resumes.",
        ]
    )


def automation_failure_message(
    signal: str, error: str, prefix: str = "Alert monitor"
) -> str:
    return "\n".join(
        [
            f"🚨 {prefix} automation failure",
            "Status: intake/debugger failed",
            f"Signal: {_clip(signal)}",
            f"Error: {_clip(error)}",
            "Next: manual attention needed; alert was not silently dropped.",
        ]
    )


def false_positive_message(
    incident_task_id: str, evidence: str, prefix: str = "Alert monitor"
) -> str:
    return "\n".join(
        [
            f"ℹ️ {prefix} closed",
            "Status: false positive / no action",
            f"Incident: {incident_task_id}",
            f"Evidence: {_clip(evidence)}",
            "Next: no follow-up queued.",
        ]
    )
