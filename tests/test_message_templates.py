from __future__ import annotations

from agent_alert_monitor.message_templates import (
    automation_failure_message,
    intake_ack_message,
    pr_opened_message,
    stalled_message,
)


def test_intake_ack_template_is_short_and_predictable() -> None:
    msg = intake_ack_message(incident_task_id="t_12345678", signal="Service 5xx critical")

    assert msg.splitlines() == [
        "🔎 Alert monitor",
        "Status: investigating",
        "Incident: t_12345678",
        "Signal: Service 5xx critical",
        "Next: debugger is checking logs/metrics.",
    ]


def test_pr_opened_template_uses_short_github_reference() -> None:
    msg = pr_opened_message(
        incident_task_id="t_incident",
        coder_task_id="t_coder",
        owner_repo="example/app",
        number=234,
    )

    assert "PR: github:example/app/pull/234" in msg
    assert "https://" not in msg
    assert len(msg.splitlines()) == 6


def test_failure_templates_never_silence_errors() -> None:
    stalled = stalled_message("t_incident", last_seen="17 minutes ago / investigating")
    failure = automation_failure_message("telegram:-100123/77", "Kanban create failed")

    assert stalled.startswith("🚨 Alert monitor stalled")
    assert "Next: watchdog will reclaim/escalate unless progress resumes." in stalled
    assert failure.startswith("🚨 Alert monitor automation failure")
    assert "Error: Kanban create failed" in failure
