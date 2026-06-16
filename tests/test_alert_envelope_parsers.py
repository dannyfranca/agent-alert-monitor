from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_alert_monitor.alert import CloudAlertParseError, SqsMessage
from agent_alert_monitor.cloud_parsers import (
    AwsEventBridgeCloudWatchAlarmParser,
    AwsSnsCloudWatchAlarmParser,
)

FIXTURES = Path(__file__).parent / "fixtures"
ALARM_ARN = (
    "arn:aws:cloudwatch:sa-east-1:123456789012:"
    "alarm:payment-processor-prod-lambda-errors-alarm"
)


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def _sqs_message_from_receive_fixture(name: str) -> SqsMessage:
    payload = _fixture(name)
    first_message = payload["Messages"][0]  # type: ignore[index]
    assert isinstance(first_message, dict)
    return SqsMessage(
        message_id=str(first_message["MessageId"]),
        receipt_handle=str(first_message["ReceiptHandle"]),
        body=str(first_message["Body"]),
        attributes=dict(first_message.get("Attributes", {})),
        message_attributes=dict(first_message.get("MessageAttributes", {})),
        raw=first_message,
    )


def _sqs_message_from_body_fixture(name: str) -> SqsMessage:
    return SqsMessage(
        message_id=f"sqs-{name}",
        receipt_handle="sanitized-receipt-handle",
        body=_fixture(name),
    )


def test_sns_cloudwatch_alarm_parser_normalizes_alarm_fixture() -> None:
    alert = AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale").parse(
        _sqs_message_from_body_fixture("aws_sns_cloudwatch_alarm_alarm.json")
    )

    assert alert.source_type == "aws_sns_cloudwatch_alarm"
    assert (
        alert.event_id
        == "sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-prod-alerts:sns-message-alarm-1"
    )
    assert alert.project_slug == "ticketdovale"
    assert alert.account_id == "123456789012"
    assert alert.region == "sa-east-1"
    assert alert.alarm_arn == ALARM_ARN
    assert alert.alarm_name == "payment-processor-prod-lambda-errors-alarm"
    assert alert.state == "ALARM"
    assert alert.previous_state == "OK"
    assert alert.state_changed_at == "2026-06-16T12:34:56Z"
    assert alert.reason and "Threshold Crossed" in alert.reason
    assert alert.reason_data["threshold"] == 1.0
    assert alert.namespace == "AWS/Lambda"
    assert alert.metric_name == "Errors"
    assert alert.dimensions == {"FunctionName": "payment-processor-prod-lambda"}
    assert alert.metadata["service"] == "payment-processor"
    assert alert.metadata["stack"] == "prod"
    assert alert.metadata["log_group"] == "/aws/lambda/payment-processor-prod-lambda"
    assert "cloudwatch_alarm_url" in alert.investigation
    assert alert.investigation["suggested_log_group"] == "/aws/lambda/payment-processor-prod-lambda"
    assert alert.raw["sns"]["MessageId"] == "sns-message-alarm-1"
    assert alert.raw["cloudwatch"]["AlarmName"] == "payment-processor-prod-lambda-errors-alarm"


def test_sns_cloudwatch_alarm_parser_normalizes_ok_fixture_and_keeps_incident_stable() -> None:
    parser = AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale")

    alarm = parser.parse(_sqs_message_from_body_fixture("aws_sns_cloudwatch_alarm_alarm.json"))
    ok = parser.parse(_sqs_message_from_body_fixture("aws_sns_cloudwatch_alarm_ok.json"))

    assert ok.state == "OK"
    assert ok.previous_state == "ALARM"
    assert ok.state_changed_at == "2026-06-16T12:39:56Z"
    assert ok.event_id.endswith(":sns-message-ok-1")
    assert alarm.incident_fingerprint == ok.incident_fingerprint
    assert alarm.transition_key != ok.transition_key
    assert ok.transition_key.endswith(":OK:2026-06-16T12:39:56Z")


def test_eventbridge_cloudwatch_alarm_parser_normalizes_alarm_fixture() -> None:
    alert = AwsEventBridgeCloudWatchAlarmParser(project_slug="ticketdovale").parse(
        _sqs_message_from_body_fixture("aws_eventbridge_cloudwatch_alarm_alarm.json")
    )

    assert alert.source_type == "aws_eventbridge_cloudwatch_alarm"
    assert alert.event_id == "eventbridge:123456789012:sa-east-1:eventbridge-alarm-1"
    assert alert.project_slug == "ticketdovale"
    assert alert.account_id == "123456789012"
    assert alert.region == "sa-east-1"
    assert alert.alarm_arn == ALARM_ARN
    assert alert.alarm_name == "payment-processor-prod-lambda-errors-alarm"
    assert alert.state == "ALARM"
    assert alert.previous_state == "OK"
    assert alert.state_changed_at == "2026-06-16T12:34:56Z"
    assert alert.reason and "Threshold Crossed" in alert.reason
    assert alert.reason_data["threshold"] == 1.0
    assert alert.namespace == "AWS/Lambda"
    assert alert.metric_name == "Errors"
    assert alert.dimensions == {"FunctionName": "payment-processor-prod-lambda"}
    assert alert.metadata["service"] == "payment-processor"
    assert alert.metadata["stack"] == "prod"
    assert alert.metadata["log_group"] == "/aws/lambda/payment-processor-prod-lambda"
    assert "cloudwatch_alarm_url" in alert.investigation
    assert alert.investigation["suggested_log_group"] == "/aws/lambda/payment-processor-prod-lambda"
    assert alert.raw["eventbridge"]["id"] == "eventbridge-alarm-1"


def test_eventbridge_cloudwatch_ok_parser_keeps_incident_stable() -> None:
    parser = AwsEventBridgeCloudWatchAlarmParser(project_slug="ticketdovale")

    alarm = parser.parse(
        _sqs_message_from_body_fixture("aws_eventbridge_cloudwatch_alarm_alarm.json")
    )
    ok = parser.parse(_sqs_message_from_body_fixture("aws_eventbridge_cloudwatch_alarm_ok.json"))

    assert ok.state == "OK"
    assert ok.previous_state == "ALARM"
    assert ok.state_changed_at == "2026-06-16T12:39:56Z"
    assert ok.event_id == "eventbridge:123456789012:sa-east-1:eventbridge-ok-1"
    assert alarm.incident_fingerprint == ok.incident_fingerprint
    assert alarm.transition_key != ok.transition_key
    assert ok.transition_key.endswith(":OK:2026-06-16T12:39:56Z")


def test_parsers_accept_sqs_receive_message_body_strings() -> None:
    sns_alert = AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale").parse(
        _sqs_message_from_receive_fixture("aws_sqs_receive_message_sns_envelope.json")
    )
    eventbridge_alert = AwsEventBridgeCloudWatchAlarmParser(project_slug="ticketdovale").parse(
        _sqs_message_from_receive_fixture("aws_sqs_receive_message_eventbridge_envelope.json")
    )

    assert sns_alert.event_id.endswith(":sns-message-alarm-1")
    assert eventbridge_alert.event_id.endswith(":eventbridge-alarm-1")


def test_sns_duplicate_event_and_transition_keys_are_deterministic() -> None:
    parser = AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale")
    message = _sqs_message_from_body_fixture("aws_sns_cloudwatch_alarm_alarm.json")

    first = parser.parse(message)
    second = parser.parse(message)

    assert first.event_id == second.event_id
    assert first.transition_key == second.transition_key
    assert first.incident_fingerprint == second.incident_fingerprint


def test_eventbridge_duplicate_event_and_transition_keys_are_deterministic() -> None:
    parser = AwsEventBridgeCloudWatchAlarmParser(project_slug="ticketdovale")
    message = _sqs_message_from_body_fixture("aws_eventbridge_cloudwatch_alarm_alarm.json")

    first = parser.parse(message)
    second = parser.parse(message)

    assert first.event_id == second.event_id
    assert first.transition_key == second.transition_key
    assert first.incident_fingerprint == second.incident_fingerprint


def test_sns_parser_error_is_explicit_and_sanitized() -> None:
    message = SqsMessage(
        message_id="sqs-bad-sns",
        receipt_handle="sanitized-receipt-handle",
        body={"Type": "Notification", "MessageId": "sns-message-1"},
    )

    with pytest.raises(CloudAlertParseError) as excinfo:
        AwsSnsCloudWatchAlarmParser(project_slug="ticketdovale").parse(message)

    assert str(excinfo.value) == "invalid aws_sns_cloudwatch_alarm message: missing TopicArn"
    assert excinfo.value.__cause__ is None
    assert "arn:aws" not in str(excinfo.value)
    assert "MessageId" not in str(excinfo.value)


def test_eventbridge_parser_error_is_explicit_and_sanitized() -> None:
    message = SqsMessage(
        message_id="sqs-bad-eventbridge",
        receipt_handle="sanitized-receipt-handle",
        body={"source": "aws.cloudwatch", "detail-type": "CloudWatch Alarm State Change"},
    )

    with pytest.raises(CloudAlertParseError) as excinfo:
        AwsEventBridgeCloudWatchAlarmParser(project_slug="ticketdovale").parse(message)

    assert str(excinfo.value) == "invalid aws_eventbridge_cloudwatch_alarm message: missing id"
    assert excinfo.value.__cause__ is None
    assert "123456789012" not in str(excinfo.value)
    assert "eventbridge" in str(excinfo.value)
