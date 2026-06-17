from __future__ import annotations

import json
from pathlib import Path

from agent_alert_monitor.alert import ParsedCloudAlert, SqsMessage
from agent_alert_monitor.cloud_parsers import AwsSnsCloudWatchAlarmParser
from agent_alert_monitor.incident_cards import IncidentCardContext, render_cloudwatch_incident_card

FIXTURES = Path(__file__).parent / "fixtures"
ALARM_ARN = (
    "arn:aws:cloudwatch:sa-east-1:123456789012:"
    "alarm:payment-processor-prod-lambda-errors-alarm"
)
ALARM_TRANSITION_KEY = (
    f"cloudwatch-transition:123456789012:sa-east-1:{ALARM_ARN}:"
    "ALARM:2026-06-16T12:34:56Z"
)
OK_TRANSITION_KEY = (
    f"cloudwatch-transition:123456789012:sa-east-1:{ALARM_ARN}:"
    "OK:2026-06-16T12:39:56Z"
)
INCIDENT_FINGERPRINT = f"cloudwatch-alarm:123456789012:sa-east-1:{ALARM_ARN}"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def _sqs_message_from_body_fixture(name: str) -> SqsMessage:
    return SqsMessage(
        message_id=f"sqs-{name}",
        receipt_handle="super-secret-receipt-handle",
        body=_fixture(name),
    )


def _context() -> IncidentCardContext:
    return IncidentCardContext(
        project_display_name="TicketDoVale",
        project_slug="ticketdovale",
        environment="prod",
        source_name="ticketdovale-prod-alerts",
        source_transport="aws_sqs",
    )


def _alarm_alert() -> ParsedCloudAlert:
    return AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale").parse(
        _sqs_message_from_body_fixture("aws_sns_cloudwatch_alarm_alarm.json")
    )


def test_alarm_incident_card_renders_required_cloudwatch_context() -> None:
    body = render_cloudwatch_incident_card(_alarm_alert(), _context())

    assert body.splitlines()[:19] == [
        "# CloudWatch Alert Incident",
        "",
        "Project: TicketDoVale (`ticketdovale`)",
        "Environment: prod",
        "Source: aws_sqs/ticketdovale-prod-alerts",
        "State: ALARM",
        "Previous state: OK",
        "State changed at: 2026-06-16T12:34:56Z",
        "Alarm: payment-processor-prod-lambda-errors-alarm",
        f"Alarm ARN: {ALARM_ARN}",
        "Service: payment-processor",
        "Log group: /aws/lambda/payment-processor-prod-lambda",
        "Region: sa-east-1",
        "Account: 123456789012",
        (
            "Event id: sns:arn:aws:sns:sa-east-1:123456789012:"
            "ticketdovale-prod-alerts:sns-message-alarm-1"
        ),
        f"Transition key: {ALARM_TRANSITION_KEY}",
        f"Incident fingerprint: {INCIDENT_FINGERPRINT}",
        "",
        "## State reason",
    ]
    assert "Threshold Crossed: 1 datapoint was greater than or equal to the threshold." in body
    assert "## Metric / trigger" in body
    assert "Namespace: AWS/Lambda" in body
    assert "Metric: Errors" in body
    assert "- FunctionName=payment-processor-prod-lambda" in body
    assert "## CloudWatch links" in body
    assert "#alarmsV2:alarm/payment-processor-prod-lambda-errors-alarm" in body
    assert "#logsV2:log-groups/log-group/$252Faws$252Flambda" in body
    assert "## Investigation commands" in body
    assert (
        "aws cloudwatch describe-alarms --region sa-east-1 "
        "--alarm-names payment-processor-prod-lambda-errors-alarm"
    ) in body
    assert (
        "aws logs filter-log-events --region sa-east-1 "
        "--log-group-name /aws/lambda/payment-processor-prod-lambda"
    ) in body
    assert "--start-time \"$(python3 -c 'import time;" in body
    assert "--filter-pattern '%ERROR|error|FATAL|panic%'" in body
    assert "<epoch-ms>" not in body
    assert "aws cloudwatch get-metric-data --region sa-east-1" in body
    assert "--metric-data-queries '[" in body
    assert '\"MetricName\":\"Errors\"' in body
    assert '\"Namespace\":\"AWS/Lambda\"' in body
    assert '\"Name\":\"FunctionName\"' in body
    assert '\"Value\":\"payment-processor-prod-lambda\"' in body
    assert "file://metric-query.json" not in body
    assert "## Required debugger protocol" in body
    assert "## Debugger output contract" in body
    assert '"type": "debugger_result"' in body
    assert '"classification": "code-fix-likely"' in body
    assert "## Coder handoff rules" in body
    assert "Only create a coder card when debugger_result.requires_coder is true." in body


def test_ok_recovery_card_is_stable_and_resolve_oriented() -> None:
    ok_alert = AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale").parse(
        _sqs_message_from_body_fixture("aws_sns_cloudwatch_alarm_ok.json")
    )

    body = render_cloudwatch_incident_card(ok_alert, _context())

    assert body.startswith("# CloudWatch Alert Recovery\n")
    assert "State: OK" in body
    assert "Previous state: ALARM" in body
    assert f"Transition key: {OK_TRANSITION_KEY}" in body
    assert (
        "Recovery note: correlate this OK transition with an open incident using "
        "the incident fingerprint before closing anything."
    ) in body
    assert "Never close/block silently; post final or blocked status first." in body


def test_card_handles_missing_optional_fields_without_none_literals() -> None:
    alert = ParsedCloudAlert(
        source_type="aws_eventbridge_cloudwatch_alarm",
        event_id="eventbridge:123456789012:sa-east-1:event-missing-optionals",
        project_slug="ticketdovale",
        account_id="123456789012",
        region="sa-east-1",
        alarm_arn=None,
        alarm_name="unknown-alarm",
        state="INSUFFICIENT_DATA",
        previous_state=None,
        state_changed_at="2026-06-16T13:00:00Z",
    )

    body = render_cloudwatch_incident_card(alert, _context())

    assert "Alarm ARN: unavailable" in body
    assert "Previous state: unavailable" in body
    assert "Service: unavailable" in body
    assert "Log group: unavailable" in body
    assert "No state reason provided by CloudWatch." in body
    assert "Namespace: unavailable" in body
    assert "Metric: unavailable" in body
    assert "- unavailable" in body
    assert "aws logs filter-log-events" not in body
    assert "aws cloudwatch get-metric-data" not in body
    assert "None" not in body


def test_card_header_uses_suggested_log_group_when_metadata_is_missing() -> None:
    alert = ParsedCloudAlert(
        source_type="aws_eventbridge_cloudwatch_alarm",
        event_id="eventbridge:123456789012:sa-east-1:event-suggested-log-group",
        project_slug="ticketdovale",
        account_id="123456789012",
        region="sa-east-1",
        alarm_arn=None,
        alarm_name="payment-processor-prod-lambda-errors-alarm",
        state="ALARM",
        previous_state="OK",
        state_changed_at="2026-06-16T13:00:00Z",
        namespace="AWS/Lambda",
        metric_name="Errors",
        investigation={"suggested_log_group": "/aws/lambda/from-investigation"},
    )

    body = render_cloudwatch_incident_card(alert, _context())

    assert "Log group: /aws/lambda/from-investigation" in body
    assert (
        "aws logs filter-log-events --region sa-east-1 "
        "--log-group-name /aws/lambda/from-investigation"
    ) in body


def test_card_uses_normalized_fields_only_and_does_not_emit_raw_secrets() -> None:
    alert = ParsedCloudAlert(
        source_type="aws_sns_cloudwatch_alarm",
        event_id="sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:safe-id",
        project_slug="ticketdovale",
        account_id="123456789012",
        region="sa-east-1",
        alarm_arn="arn:aws:cloudwatch:sa-east-1:123456789012:alarm:safe-alarm",
        alarm_name="safe-alarm",
        state="ALARM",
        previous_state="OK",
        state_changed_at="2026-06-16T13:01:00Z",
        reason="Safe normalized reason",
        namespace="AWS/Lambda",
        metric_name="Errors",
        dimensions={"FunctionName": "safe-lambda"},
        metadata={"service": "safe", "log_group": "/aws/lambda/safe-lambda"},
        raw={
            "sns": {
                "Signature": "TOP_SECRET_SIGNATURE",
                "UnsubscribeURL": "https://example.invalid/token=TOP_SECRET_TOKEN",
            },
            "sqs": {"ReceiptHandle": "TOP_SECRET_RECEIPT"},
        },
    )

    body = render_cloudwatch_incident_card(alert, _context())

    assert "Safe normalized reason" in body
    assert "TOP_SECRET" not in body
    assert "UnsubscribeURL" not in body
    assert "ReceiptHandle" not in body
    assert "Signature" not in body
