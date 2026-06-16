from __future__ import annotations

from agent_alert_monitor.alert import (
    ParsedCloudAlert,
    SqsMessage,
    cloudwatch_incident_fingerprint,
    cloudwatch_transition_key,
    event_id_for_sqs_message,
)


def test_sns_sqs_message_event_id_uses_topic_arn_and_message_id() -> None:
    message = SqsMessage(
        message_id="sqs-delivery-1",
        receipt_handle="receipt",
        body={
            "Type": "Notification",
            "TopicArn": "arn:aws:sns:sa-east-1:123456789012:ticketdovale-alerts",
            "MessageId": "sns-message-1",
            "Message": "{}",
        },
    )

    assert (
        event_id_for_sqs_message(message, source_type="aws_sns_cloudwatch_alarm")
        == "sns:arn:aws:sns:sa-east-1:123456789012:ticketdovale-alerts:sns-message-1"
    )


def test_eventbridge_sqs_message_event_id_uses_account_region_and_event_id() -> None:
    message = SqsMessage(
        message_id="sqs-delivery-1",
        receipt_handle="receipt",
        body={
            "id": "eventbridge-event-1",
            "account": "123456789012",
            "region": "sa-east-1",
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
        },
    )

    assert (
        event_id_for_sqs_message(message, source_type="aws_eventbridge_cloudwatch_alarm")
        == "eventbridge:123456789012:sa-east-1:eventbridge-event-1"
    )


def test_cloudwatch_transition_and_incident_keys_are_state_aware_but_recovery_stable() -> None:
    alarm = ParsedCloudAlert(
        source_type="aws_sns_cloudwatch_alarm",
        event_id="sns:topic:alarm-delivery",
        project_slug="ticketdovale",
        account_id="123456789012",
        region="sa-east-1",
        alarm_arn="arn:aws:cloudwatch:sa-east-1:123456789012:alarm:PaymentErrors",
        alarm_name="PaymentErrors",
        state="ALARM",
        previous_state="OK",
        state_changed_at="2026-06-16T12:34:56Z",
        reason="Threshold crossed",
        namespace="AWS/Lambda",
        metric_name="Errors",
        dimensions={"FunctionName": "payment-processor-prod-lambda"},
        investigation={"suggested_log_group": "/aws/lambda/payment-processor-prod-lambda"},
        raw={"transport": "sns"},
    )
    ok = ParsedCloudAlert(
        source_type=alarm.source_type,
        event_id="sns:topic:ok-delivery",
        project_slug=alarm.project_slug,
        account_id=alarm.account_id,
        region=alarm.region,
        alarm_arn=alarm.alarm_arn,
        alarm_name=alarm.alarm_name,
        state="OK",
        previous_state="ALARM",
        state_changed_at="2026-06-16T12:39:56Z",
        reason="Recovered",
        namespace=alarm.namespace,
        metric_name=alarm.metric_name,
        dimensions=alarm.dimensions,
        investigation=alarm.investigation,
        raw={"transport": "sns"},
    )

    assert alarm.transition_key == cloudwatch_transition_key(alarm)
    assert alarm.transition_key == (
        "cloudwatch-transition:123456789012:sa-east-1:"
        "arn:aws:cloudwatch:sa-east-1:123456789012:alarm:PaymentErrors:"
        "ALARM:2026-06-16T12:34:56Z"
    )
    assert ok.transition_key.endswith(":OK:2026-06-16T12:39:56Z")
    assert alarm.transition_key != ok.transition_key
    assert alarm.incident_fingerprint == ok.incident_fingerprint
    assert alarm.incident_fingerprint == cloudwatch_incident_fingerprint(alarm)
    assert alarm.incident_fingerprint == (
        "cloudwatch-alarm:123456789012:sa-east-1:"
        "arn:aws:cloudwatch:sa-east-1:123456789012:alarm:PaymentErrors"
    )


def test_incident_fingerprint_falls_back_to_alarm_name_when_arn_is_absent() -> None:
    alert = ParsedCloudAlert(
        source_type="aws_eventbridge_cloudwatch_alarm",
        event_id="eventbridge:123:sa-east-1:event-1",
        project_slug="ticketdovale",
        account_id="123456789012",
        region="sa-east-1",
        alarm_arn=None,
        alarm_name="PaymentErrors",
        state="ALARM",
        previous_state="OK",
        state_changed_at="2026-06-16T12:34:56Z",
    )

    assert alert.incident_fingerprint == "cloudwatch-alarm:123456789012:sa-east-1:PaymentErrors"
