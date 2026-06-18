from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_alert_monitor.alert import SqsMessage
from agent_alert_monitor.cloud_parsers import AwsSnsCloudWatchAlarmParser
from agent_alert_monitor.config import load_config
from agent_alert_monitor.kanban import KanbanCardRequest
from agent_alert_monitor.ledger import AlertLedger
from agent_alert_monitor.lifecycle import (
    DebuggerResultValidationError,
    evaluate_pr_suitability,
    mark_awaiting_pr,
    record_pr_feedback_if_unsuitable,
    record_pr_reference,
    sync_debugger_result,
    validate_debugger_result,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeKanbanClient:
    def __init__(self) -> None:
        self.created: list[KanbanCardRequest] = []
        self.comments: list[tuple[str, str]] = []

    def create_incident(self, request: KanbanCardRequest) -> str:
        self.created.append(request)
        return f"t_coder_{len(self.created)}"

    def comment(self, task_id: str, body: str) -> None:
        self.comments.append((task_id, body))


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _alert(name: str = "aws_sns_cloudwatch_alarm_alarm.json"):
    message = SqsMessage(
        message_id=f"sqs-{name}",
        receipt_handle="sanitized-receipt-handle",
        body=_fixture(name),
        raw={"fixture": name},
    )
    return AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale").parse(message)


def _cfg(tmp_path: Path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: {tmp_path / "state"}
projects:
  - slug: ticketdovale
    display_name: TicketDoVale
    environment: prod
    sources:
      - name: ticketdovale-prod-alerts
        type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    sinks:
      - name: ticketdovale-telegram-status
        type: telegram
        chat_id: "-100111"
    hermes:
      coordinator_profile: alert-coordinator
      kanban_board: alerts
    kanban:
      incident_assignee: debugger
      coder_assignee: coder
      tenant: ticketdovale
""".strip(),
        encoding="utf-8",
    )
    return load_config(
        config_file,
        project_slug="ticketdovale",
        env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
    )


def _opened_incident(tmp_path: Path) -> tuple[Any, AlertLedger, str]:
    cfg = _cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    alert = _alert()
    message = SqsMessage(
        message_id="sqs-alarm-1",
        receipt_handle="sanitized-receipt-handle",
        body=_fixture("aws_sns_cloudwatch_alarm_alarm.json"),
        raw={"fixture": "alarm"},
    )
    opened = ledger.process_cloud_alert("ticketdovale-prod-alerts", message, alert)
    assert opened.incident_id is not None
    ledger.attach_cloud_incident_kanban_task(opened.incident_id, "t_incident_1")
    return cfg, ledger, opened.incident_id


def _debugger_result(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "debugger_result",
        "incident_id": "inc_placeholder",
        "classification": "code-fix-likely",
        "confidence": "medium",
        "evidence": [
            {
                "source": "cloudwatch_logs",
                "summary": "5 payment webhook timeouts after deploy",
                "time_window": "2026-06-16T12:00:00Z/2026-06-16T12:30:00Z",
            }
        ],
        "suspected_component": "payment processor webhook handling",
        "recommended_next_action": "queue coder card",
        "requires_coder": True,
        "requires_human": False,
        "telegram_status": {
            "status": "code fix likely; coder queued",
            "evidence": "Recent errors point to payment webhook timeout path.",
        },
    }
    payload.update(overrides)
    return payload


def test_debugger_result_validation_rejects_unstructured_or_mismatched_payloads(
    tmp_path: Path,
) -> None:
    _cfg_obj, _ledger, incident_id = _opened_incident(tmp_path)

    with pytest.raises(DebuggerResultValidationError, match="type"):
        validate_debugger_result({"classification": "code-fix-likely"}, incident_id)
    with pytest.raises(DebuggerResultValidationError, match="incident_id"):
        validate_debugger_result(_debugger_result(incident_id="inc_other"), incident_id)
    with pytest.raises(DebuggerResultValidationError, match="classification"):
        validate_debugger_result(
            _debugger_result(incident_id=incident_id, classification="maybe"), incident_id
        )
    with pytest.raises(DebuggerResultValidationError, match="evidence"):
        validate_debugger_result(
            _debugger_result(incident_id=incident_id, evidence=[]), incident_id
        )
    with pytest.raises(DebuggerResultValidationError, match="telegram_status"):
        validate_debugger_result(
            _debugger_result(incident_id=incident_id, telegram_status=None), incident_id
        )

    result = validate_debugger_result(_debugger_result(incident_id=incident_id), incident_id)
    assert result.classification == "code-fix-likely"
    assert result.requires_coder is True
    assert result.evidence[0].summary == "5 payment webhook timeouts after deploy"


def test_self_recovered_debugger_result_posts_final_status_before_resolving(
    tmp_path: Path,
) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    kanban = FakeKanbanClient()
    telegram_messages: list[str] = []

    result = sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=kanban,
        incident_id=incident_id,
        payload=_debugger_result(
            incident_id=incident_id,
            classification="self-recovered/transient",
            requires_coder=False,
            suspected_component=None,
            recommended_next_action="resolve incident",
            telegram_status={
                "status": "self-recovered; no code change needed",
                "evidence": "Alarm returned to OK and logs are clean for 20 minutes.",
            },
        ),
        status_sender=lambda _cfg, text: telegram_messages.append(text),
    )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert result.action == "resolved"
    assert incident is not None
    assert incident.status == "resolved"
    assert incident.last_channel_status == "final"
    assert telegram_messages
    assert "self-recovered" in telegram_messages[0]
    assert "logs are clean" in telegram_messages[0]
    assert kanban.comments and kanban.comments[-1][0] == "t_incident_1"


def test_self_recovered_debugger_result_can_finalize_sqs_recovered_incident(
    tmp_path: Path,
) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    with ledger.connect() as conn:
        conn.execute(
            "UPDATE alert_incidents SET status='self_recovered' WHERE incident_id=?",
            (incident_id,),
        )
    telegram_messages: list[str] = []

    result = sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        incident_id=incident_id,
        payload=_debugger_result(
            incident_id=incident_id,
            classification="self-recovered/transient",
            requires_coder=False,
            suspected_component=None,
            recommended_next_action="resolve incident",
            telegram_status={
                "status": "self-recovered; final evidence posted",
                "evidence": "CloudWatch already recovered before debugger handoff.",
            },
        ),
        status_sender=lambda _cfg, text: telegram_messages.append(text),
    )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert result.action == "resolved"
    assert incident is not None
    assert incident.status == "resolved"
    assert incident.last_channel_status == "final"
    assert telegram_messages and "final evidence" in telegram_messages[0]


def test_self_recovered_without_final_channel_evidence_does_not_close_incident(
    tmp_path: Path,
) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)

    with pytest.raises(DebuggerResultValidationError, match="telegram_status"):
        sync_debugger_result(
            cfg,
            ledger=ledger,
            kanban_client=FakeKanbanClient(),
            incident_id=incident_id,
            payload=_debugger_result(
                incident_id=incident_id,
                classification="self-recovered/transient",
                requires_coder=False,
                telegram_status=None,
            ),
            status_sender=lambda _cfg, _text: None,
        )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert incident is not None
    assert incident.status == "investigating"


def test_code_fix_likely_creates_bounded_coder_card_and_tracks_task_id(
    tmp_path: Path,
) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    kanban = FakeKanbanClient()
    telegram_messages: list[str] = []

    result = sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=kanban,
        incident_id=incident_id,
        payload=_debugger_result(incident_id=incident_id),
        status_sender=lambda _cfg, text: telegram_messages.append(text),
    )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert result.action == "coder_queued"
    assert incident is not None
    assert incident.status == "code_fix_queued"
    assert incident.coder_task_id == "t_coder_1"
    assert incident.last_channel_status == "progress"
    assert len(kanban.created) == 1
    card = kanban.created[0]
    assert card.assignee == "coder"
    assert f"Parent incident: {incident_id}" in card.body
    assert "5 payment webhook timeouts after deploy" in card.body
    assert "payment processor webhook handling" in card.body
    assert "Do not change alert thresholds as a first response." in card.body
    assert "Original incident marker" in card.body
    assert telegram_messages and "coder queued" in telegram_messages[0]


def test_record_pr_reference_tracks_transition_without_closing_incident(tmp_path: Path) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        incident_id=incident_id,
        payload=_debugger_result(incident_id=incident_id),
        status_sender=lambda _cfg, _text: None,
    )

    result = record_pr_reference(
        ledger,
        incident_id=incident_id,
        pr_ref="github:dannyvcfranca/ticketdovale/pull/123",
        status="pr_opened",
    )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert result.action == "pr_ref_updated"
    assert incident is not None
    assert incident.status == "pr_opened"
    assert incident.pr_ref == "github:dannyvcfranca/ticketdovale/pull/123"


def test_mark_awaiting_pr_tracks_intermediate_coder_lifecycle_state(tmp_path: Path) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        incident_id=incident_id,
        payload=_debugger_result(incident_id=incident_id),
        status_sender=lambda _cfg, _text: None,
    )

    result = mark_awaiting_pr(ledger, incident_id=incident_id)

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert result.action == "awaiting_pr_marked"
    assert incident is not None
    assert incident.status == "awaiting_pr"
    assert incident.pr_ref is None


def test_pr_lifecycle_transitions_reject_non_coder_incidents(tmp_path: Path) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        incident_id=incident_id,
        payload=_debugger_result(
            incident_id=incident_id,
            classification="self-recovered/transient",
            requires_coder=False,
            suspected_component=None,
            recommended_next_action="resolve incident",
            telegram_status={
                "status": "self-recovered; no code change needed",
                "evidence": "Alarm returned to OK and logs are clean for 20 minutes.",
            },
        ),
        status_sender=lambda _cfg, _text: None,
    )

    with pytest.raises(ValueError, match="active coder lifecycle"):
        record_pr_reference(
            ledger,
            incident_id=incident_id,
            pr_ref="github:dannyvcfranca/ticketdovale/pull/123",
        )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert incident is not None
    assert incident.status == "resolved"
    assert incident.pr_ref is None


def test_stale_debugger_result_cannot_reopen_terminal_incident(tmp_path: Path) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        incident_id=incident_id,
        payload=_debugger_result(
            incident_id=incident_id,
            classification="self-recovered/transient",
            requires_coder=False,
            suspected_component=None,
            recommended_next_action="resolve incident",
            telegram_status={
                "status": "self-recovered; no code change needed",
                "evidence": "Alarm returned to OK and logs are clean for 20 minutes.",
            },
        ),
        status_sender=lambda _cfg, _text: None,
    )
    kanban = FakeKanbanClient()

    with pytest.raises(ValueError, match="active debugger lifecycle"):
        sync_debugger_result(
            cfg,
            ledger=ledger,
            kanban_client=kanban,
            incident_id=incident_id,
            payload=_debugger_result(incident_id=incident_id),
            status_sender=lambda _cfg, _text: None,
        )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert incident is not None
    assert incident.status == "resolved"
    assert kanban.created == []


def test_pr_reference_rejects_non_pr_ref_status(tmp_path: Path) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        incident_id=incident_id,
        payload=_debugger_result(incident_id=incident_id),
        status_sender=lambda _cfg, _text: None,
    )

    with pytest.raises(ValueError, match="PR reference status"):
        record_pr_reference(
            ledger,
            incident_id=incident_id,
            pr_ref="github:dannyvcfranca/ticketdovale/pull/123",
            status="awaiting_pr",  # type: ignore[arg-type]
        )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert incident is not None
    assert incident.status == "code_fix_queued"
    assert incident.pr_ref is None


def test_unsuitable_pr_posts_visible_feedback_and_does_not_close_incident(
    tmp_path: Path,
) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        incident_id=incident_id,
        payload=_debugger_result(incident_id=incident_id),
        status_sender=lambda _cfg, _text: None,
    )
    kanban = FakeKanbanClient()
    telegram_messages: list[str] = []
    evaluation = evaluate_pr_suitability(
        incident_id=incident_id,
        pr_ref="github:dannyvcfranca/ticketdovale/pull/123",
        changed_files=["deployment/alarms.ts", "README.md"],
        tests=[] ,
        incident_refs=[],
        evidence_summary="",
        suppresses_alarm=True,
        threshold_change=True,
        threshold_evidence="",
        unrelated_changes=["README.md"],
        touches_secrets_iam_or_deploy=True,
    )

    result = record_pr_feedback_if_unsuitable(
        cfg,
        ledger=ledger,
        kanban_client=kanban,
        incident_id=incident_id,
        evaluation=evaluation,
        status_sender=lambda _cfg, text: telegram_messages.append(text),
    )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert evaluation.suitable is False
    assert result.action == "unsuitable_pr_feedback_posted"
    assert incident is not None
    assert incident.status == "awaiting_review"
    assert incident.pr_ref == "github:dannyvcfranca/ticketdovale/pull/123"
    assert incident.last_channel_status == "progress"
    assert incident.status not in {"resolved", "closed"}
    assert kanban.comments and "coder fix not accepted" in kanban.comments[0][1]
    assert "missing incident reference" in kanban.comments[0][1]
    assert telegram_messages and "coder fix not accepted" in telegram_messages[0]


def test_unsuitable_pr_feedback_rejects_missing_pr_ref_before_side_effects(tmp_path: Path) -> None:
    cfg, ledger, incident_id = _opened_incident(tmp_path)
    sync_debugger_result(
        cfg,
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        incident_id=incident_id,
        payload=_debugger_result(incident_id=incident_id),
        status_sender=lambda _cfg, _text: None,
    )
    kanban = FakeKanbanClient()
    evaluation = evaluate_pr_suitability(
        incident_id=incident_id,
        pr_ref="   ",
        changed_files=["internal/payment.py"],
        tests=["pytest tests/test_payment.py"],
        incident_refs=[incident_id],
        evidence_summary="fixes timeout path",
    )

    with pytest.raises(ValueError, match="pr_ref is required"):
        record_pr_feedback_if_unsuitable(
            cfg,
            ledger=ledger,
            kanban_client=kanban,
            incident_id=incident_id,
            evaluation=evaluation,
            status_sender=lambda _cfg, _text: None,
        )

    incident = ledger.get_cloud_incident_by_id(incident_id)
    assert incident is not None
    assert incident.pr_ref is None
    assert kanban.comments == []
