from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from agent_alert_monitor.alert import CloudAlertParseError, ParsedCloudAlert, SqsMessage


class AwsSnsCloudWatchAlarmParser:
    source_type = "aws_sns_cloudwatch_alarm"

    def __init__(self, *, project_slug: str) -> None:
        self.project_slug = project_slug

    def parse(self, raw_sqs_message: SqsMessage) -> ParsedCloudAlert:
        try:
            sns_envelope = _message_body_json(raw_sqs_message)
            topic_arn = _required_str(sns_envelope, "TopicArn")
            sns_message_id = _required_str(sns_envelope, "MessageId")
            cloudwatch_alarm = _json_object_from_string(
                _required_str(sns_envelope, "Message"), field="Message"
            )

            alarm_name = _required_str(cloudwatch_alarm, "AlarmName")
            alarm_arn = _optional_str(cloudwatch_alarm.get("AlarmArn"))
            account_id = _required_str(cloudwatch_alarm, "AWSAccountId")
            region = _region_from_alarm_arn(alarm_arn) or _required_region_from_topic_arn(topic_arn)
            state = _required_str(cloudwatch_alarm, "NewStateValue")
            state_changed_at = _required_str(cloudwatch_alarm, "StateChangeTime")
            previous_state = _optional_str(cloudwatch_alarm.get("OldStateValue"))
            trigger = _optional_mapping(cloudwatch_alarm.get("Trigger"))
            dimensions = _dimensions_from_sns_trigger(trigger)
            namespace = _optional_str(trigger.get("Namespace")) if trigger else None
            metric_name = _optional_str(trigger.get("MetricName")) if trigger else None
            metadata = _metadata_from_dimensions(alarm_name=alarm_name, dimensions=dimensions)

            alert = ParsedCloudAlert(
                source_type=self.source_type,
                event_id=f"sns:{topic_arn}:{sns_message_id}",
                project_slug=self.project_slug,
                account_id=account_id,
                region=region,
                alarm_arn=alarm_arn,
                alarm_name=alarm_name,
                state=state,
                previous_state=previous_state,
                state_changed_at=state_changed_at,
                reason=_optional_str(cloudwatch_alarm.get("NewStateReason")),
                reason_data=_json_object_from_optional_string(
                    cloudwatch_alarm.get("NewStateReasonData"), field="NewStateReasonData"
                ),
                namespace=namespace,
                metric_name=metric_name,
                dimensions=dimensions,
                metadata=metadata,
                investigation=_investigation_context(
                    region=region,
                    alarm_name=alarm_name,
                    alarm_arn=alarm_arn,
                    suggested_log_group=_optional_str(metadata.get("log_group")),
                ),
                raw={"sns": sns_envelope, "cloudwatch": cloudwatch_alarm},
            )
        except CloudAlertParseError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CloudAlertParseError(
                f"invalid {self.source_type} message: {_safe_parse_error(exc)}"
            ) from None
        else:
            return alert


class AwsEventBridgeCloudWatchAlarmParser:
    source_type = "aws_eventbridge_cloudwatch_alarm"

    def __init__(self, *, project_slug: str) -> None:
        self.project_slug = project_slug

    def parse(self, raw_sqs_message: SqsMessage) -> ParsedCloudAlert:
        try:
            event = _message_body_json(raw_sqs_message)
            event_id = _required_str(event, "id")
            account_id = _required_str(event, "account")
            region = _required_str(event, "region")
            detail = _required_mapping(event, "detail")
            state = _required_mapping(detail, "state")

            alarm_name = _required_str(detail, "alarmName")
            alarm_arn = _cloudwatch_alarm_arn_from_eventbridge(event)
            current_state = _required_str(state, "value")
            state_changed_at = _required_str(state, "timestamp")
            previous_state = _previous_state_value(detail)
            metric = _metric_from_eventbridge_detail(detail)
            dimensions = _dimensions_from_eventbridge_metric(metric)
            namespace = _optional_str(metric.get("namespace")) if metric else None
            metric_name = _optional_str(metric.get("name")) if metric else None
            metadata = _metadata_from_dimensions(alarm_name=alarm_name, dimensions=dimensions)

            alert = ParsedCloudAlert(
                source_type=self.source_type,
                event_id=f"eventbridge:{account_id}:{region}:{event_id}",
                project_slug=self.project_slug,
                account_id=account_id,
                region=region,
                alarm_arn=alarm_arn,
                alarm_name=alarm_name,
                state=current_state,
                previous_state=previous_state,
                state_changed_at=state_changed_at,
                reason=_optional_str(state.get("reason")),
                reason_data=_json_object_from_optional_string(
                    state.get("reasonData"), field="detail.state.reasonData"
                ),
                namespace=namespace,
                metric_name=metric_name,
                dimensions=dimensions,
                metadata=metadata,
                investigation=_investigation_context(
                    region=region,
                    alarm_name=alarm_name,
                    alarm_arn=alarm_arn,
                    suggested_log_group=_optional_str(metadata.get("log_group")),
                ),
                raw={"eventbridge": event},
            )
        except CloudAlertParseError:
            raise
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CloudAlertParseError(
                f"invalid {self.source_type} message: {_safe_parse_error(exc)}"
            ) from None
        else:
            return alert


def _message_body_json(raw_sqs_message: SqsMessage) -> dict[str, Any]:
    try:
        return raw_sqs_message.body_json
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("invalid SQS message body JSON") from None


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if value is None or value == "":
        raise ValueError(f"missing {key}")
    if not isinstance(value, str):
        raise ValueError(f"invalid {key}")
    return value


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _required_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"missing {key}")
    return value


def _optional_mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _json_object_from_string(value: str, *, field: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"invalid {field}")
    return parsed


def _json_object_from_optional_string(value: object, *, field: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError(f"invalid {field}")
    return _json_object_from_string(value, field=field)


def _region_from_alarm_arn(alarm_arn: str | None) -> str | None:
    if not alarm_arn:
        return None
    parts = alarm_arn.split(":", 5)
    if len(parts) >= 4 and parts[0] == "arn" and parts[2] == "cloudwatch" and parts[3]:
        return parts[3]
    return None


def _required_region_from_topic_arn(topic_arn: str) -> str:
    parts = topic_arn.split(":", 5)
    if len(parts) >= 4 and parts[0] == "arn" and parts[2] == "sns" and parts[3]:
        return parts[3]
    raise ValueError("missing region")


def _dimensions_from_sns_trigger(trigger: dict[str, Any]) -> dict[str, str]:
    dimensions = trigger.get("Dimensions") if trigger else None
    if isinstance(dimensions, dict):
        return {str(key): str(value) for key, value in dimensions.items() if value is not None}
    if isinstance(dimensions, list):
        parsed: dict[str, str] = {}
        for item in dimensions:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("Name")
            value = item.get("value") or item.get("Value")
            if name is not None and value is not None:
                parsed[str(name)] = str(value)
        return parsed
    return {}


def _cloudwatch_alarm_arn_from_eventbridge(event: dict[str, Any]) -> str | None:
    resources = event.get("resources")
    if isinstance(resources, list):
        for resource in resources:
            if isinstance(resource, str) and ":cloudwatch:" in resource and ":alarm:" in resource:
                return resource
    detail = event.get("detail")
    if isinstance(detail, dict):
        return _optional_str(detail.get("alarmArn"))
    return None


def _previous_state_value(detail: dict[str, Any]) -> str | None:
    previous_state = detail.get("previousState")
    if isinstance(previous_state, dict):
        return _optional_str(previous_state.get("value"))
    return None


def _metric_from_eventbridge_detail(detail: dict[str, Any]) -> dict[str, Any]:
    configuration = detail.get("configuration")
    if not isinstance(configuration, dict):
        return {}
    metrics = configuration.get("metrics")
    if not isinstance(metrics, list):
        return {}
    for entry in metrics:
        if not isinstance(entry, dict):
            continue
        metric_stat = entry.get("metricStat")
        if not isinstance(metric_stat, dict):
            continue
        metric = metric_stat.get("metric")
        if isinstance(metric, dict):
            return metric
    return {}


def _dimensions_from_eventbridge_metric(metric: dict[str, Any]) -> dict[str, str]:
    dimensions = metric.get("dimensions") if metric else None
    if isinstance(dimensions, dict):
        return {str(key): str(value) for key, value in dimensions.items() if value is not None}
    return {}


def _metadata_from_dimensions(*, alarm_name: str, dimensions: dict[str, str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    function_name = dimensions.get("FunctionName")
    if function_name:
        metadata["log_group"] = f"/aws/lambda/{function_name}"
        name_parts = function_name.split("-")
        core_parts = name_parts[:-1] if name_parts[-1:] == ["lambda"] else name_parts
        if len(core_parts) >= 2:
            metadata["stack"] = core_parts[-1]
            metadata["service"] = "-".join(core_parts[:-1])
        else:
            metadata["service"] = function_name
    alarm_type = _alarm_type_from_name(alarm_name, function_name=function_name)
    if alarm_type:
        metadata["alarm_type"] = alarm_type
    return metadata


def _alarm_type_from_name(alarm_name: str, *, function_name: str | None) -> str | None:
    suffix = alarm_name
    if function_name and suffix.startswith(function_name):
        suffix = suffix[len(function_name) :]
    suffix = suffix.strip("-_")
    if suffix.endswith("-alarm"):
        suffix = suffix[: -len("-alarm")]
    suffix = suffix.strip("-_")
    return suffix.replace("-", "_") if suffix else None


def _investigation_context(
    *,
    region: str,
    alarm_name: str,
    alarm_arn: str | None,
    suggested_log_group: str | None,
) -> dict[str, Any]:
    encoded_alarm = quote(alarm_name, safe="")
    context: dict[str, Any] = {
        "cloudwatch_alarm_url": (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
            f"#alarmsV2:alarm/{encoded_alarm}"
        ),
        "suggested_time_window_minutes": 30,
    }
    if alarm_arn:
        context["alarm_arn"] = alarm_arn
    if suggested_log_group:
        context["suggested_log_group"] = suggested_log_group
        encoded_log_group = quote(suggested_log_group, safe="")
        context["cloudwatch_logs_url"] = (
            f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
            f"#logsV2:log-groups/log-group/{encoded_log_group}"
        )
    return context


def _safe_parse_error(exc: BaseException) -> str:
    message = str(exc)
    allowed_prefixes = ("missing ", "invalid ")
    if message.startswith(allowed_prefixes):
        return message
    if isinstance(exc, json.JSONDecodeError):
        return "invalid JSON"
    return exc.__class__.__name__

