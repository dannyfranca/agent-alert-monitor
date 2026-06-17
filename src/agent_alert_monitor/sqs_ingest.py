from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any, Protocol

from .alert import AlertEnvelopeParser, CloudAlertParseError, ParsedCloudAlert, SqsMessage
from .cloud_parsers import AwsEventBridgeCloudWatchAlarmParser, AwsSnsCloudWatchAlarmParser
from .config import AgentConfig, AwsSqsSourceConfig


class SqsClient(Protocol):
    def get_queue_attributes(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def receive_message(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def delete_message(self, **kwargs: Any) -> Mapping[str, Any]: ...


class Boto3SqsClient:
    def __init__(self, *, region_name: str) -> None:
        try:
            import boto3  # type: ignore[import-not-found, import-untyped]
        except ImportError as exc:  # pragma: no cover - exercised only without optional runtime dep
            raise RuntimeError(
                "sqs commands require boto3; install agent-alert-monitor with AWS dependencies"
            ) from exc
        self._client = boto3.client("sqs", region_name=region_name)

    def receive_message(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._client.receive_message(**kwargs)

    def get_queue_attributes(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._client.get_queue_attributes(**kwargs)

    def delete_message(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._client.delete_message(**kwargs)


def find_sqs_source(cfg: AgentConfig, source_name: str) -> AwsSqsSourceConfig:
    source = next(
        (
            source
            for source in cfg.project.sources
            if isinstance(source, AwsSqsSourceConfig) and source.name == source_name
        ),
        None,
    )
    if source is None:
        raise ValueError(f"unknown aws_sqs source for project {cfg.project_slug}: {source_name}")
    _validate_sqs_source(source, project_slug=cfg.project_slug)
    return source


def _validate_sqs_source(source: AwsSqsSourceConfig, *, project_slug: str) -> None:
    if not source.queue_url:
        raise ValueError(
            "missing queue URL environment variable for "
            f"project {project_slug} source {source.name}: {source.queue_url_env}"
        )
    if not source.region:
        raise ValueError(f"missing region for project {project_slug} source {source.name}")
    if source.envelope not in {
        "aws_sns_cloudwatch_alarm",
        "aws_eventbridge_cloudwatch_alarm",
    }:
        raise ValueError(f"unsupported SQS source envelope: {source.envelope}")


def receive_and_parse_sqs_messages(
    cfg: AgentConfig,
    *,
    source_name: str,
    max_messages: int | None = None,
    dry_run: bool = True,
    client: SqsClient | None = None,
) -> dict[str, Any]:
    source = find_sqs_source(cfg, source_name)
    if not dry_run:
        raise ValueError("live sqs-ingest is not implemented yet; pass --dry-run")
    effective_max_messages = _effective_max_messages(
        max_messages if max_messages is not None else source.max_messages
    )
    sqs_client = client or Boto3SqsClient(region_name=source.region)
    try:
        response = sqs_client.receive_message(
            QueueUrl=source.queue_url,
            MaxNumberOfMessages=effective_max_messages,
            WaitTimeSeconds=source.wait_time_seconds,
            VisibilityTimeout=source.visibility_timeout_seconds,
            AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
            MessageAttributeNames=["All"],
        )
    except Exception as exc:
        raise RuntimeError(
            f"SQS receive failed for source {source.name}: {_safe_client_error(exc)}"
        ) from None
    raw_messages = response.get("Messages", [])
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise ValueError("SQS ReceiveMessage returned invalid Messages payload")

    parser = _parser_for_source(source, project_slug=cfg.project_slug)
    messages = [_parse_sqs_message(raw_message, parser=parser) for raw_message in raw_messages]
    return {
        "project": cfg.project_slug,
        "source": source.name,
        "queue_url_env": source.queue_url_env,
        "region": source.region,
        "envelope": source.envelope,
        "dry_run": True,
        "deletes_messages": False,
        "mutates_incidents": False,
        "messages_received": len(messages),
        "messages": messages,
    }


def inspect_dlq_messages(
    cfg: AgentConfig,
    *,
    source_name: str,
    max_messages: int | None = None,
    client: SqsClient | None = None,
) -> dict[str, Any]:
    source = find_sqs_source(cfg, source_name)
    if not source.dlq_queue_url:
        raise ValueError(f"missing DLQ URL for source {source.name}: {source.dlq_queue_url_env}")
    effective_max_messages = _effective_max_messages(
        max_messages if max_messages is not None else source.max_messages
    )
    sqs_client = client or Boto3SqsClient(region_name=source.region)
    try:
        response = sqs_client.receive_message(
            QueueUrl=source.dlq_queue_url,
            MaxNumberOfMessages=effective_max_messages,
            WaitTimeSeconds=0,
            VisibilityTimeout=0,
            AttributeNames=["ApproximateReceiveCount", "SentTimestamp"],
            MessageAttributeNames=["All"],
        )
    except Exception as exc:
        raise RuntimeError(
            f"SQS DLQ receive failed for source {source.name}: {_safe_client_error(exc)}"
        ) from None
    raw_messages = response.get("Messages", [])
    if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, (str, bytes)):
        raise ValueError("SQS ReceiveMessage returned invalid Messages payload")

    parser = _parser_for_source(source, project_slug=cfg.project_slug)
    return {
        "project": cfg.project_slug,
        "source": source.name,
        "dlq_url_env": source.dlq_queue_url_env,
        "region": source.region,
        "envelope": source.envelope,
        "messages_received": len(raw_messages),
        "messages": [
            _inspect_dlq_message(raw_message, parser=parser) for raw_message in raw_messages
        ],
    }


def _effective_max_messages(value: int) -> int:
    if value < 1 or value > 10:
        raise ValueError("SQS max messages must be between 1 and 10")
    return value


def _parser_for_source(source: AwsSqsSourceConfig, *, project_slug: str) -> AlertEnvelopeParser:
    if source.envelope == "aws_sns_cloudwatch_alarm":
        return AwsSnsCloudWatchAlarmParser(project_slug=project_slug)
    if source.envelope == "aws_eventbridge_cloudwatch_alarm":
        return AwsEventBridgeCloudWatchAlarmParser(project_slug=project_slug)
    raise ValueError(f"unsupported SQS source envelope: {source.envelope}")


def _parse_sqs_message(raw_message: object, *, parser: AlertEnvelopeParser) -> dict[str, Any]:
    if not isinstance(raw_message, Mapping):
        return {"ok": False, "message_id": "", "error": "invalid SQS message payload"}
    message_id = str(raw_message.get("MessageId") or "")
    sqs_message = SqsMessage(
        message_id=message_id,
        receipt_handle=_optional_str(raw_message.get("ReceiptHandle")),
        body=raw_message.get("Body", ""),
        attributes=_string_mapping(raw_message.get("Attributes")),
        message_attributes=_string_mapping(raw_message.get("MessageAttributes")),
        raw=dict(raw_message),
    )
    try:
        alert = parser.parse(sqs_message)
    except CloudAlertParseError as exc:
        return {"ok": False, "message_id": message_id, "error": str(exc)}
    return _parsed_alert_row(message_id=message_id, alert=alert)


def _inspect_dlq_message(raw_message: object, *, parser: AlertEnvelopeParser) -> dict[str, Any]:
    if not isinstance(raw_message, Mapping):
        return {"ok": False, "message_id": "", "parser_error": "invalid SQS message payload"}
    parsed = _parse_sqs_message(raw_message, parser=parser)
    row: dict[str, Any] = {
        "ok": bool(parsed.get("ok")),
        "message_id": str(raw_message.get("MessageId") or ""),
        "receive_count": _int_mapping_value(
            raw_message.get("Attributes"), "ApproximateReceiveCount"
        ),
        "sent_timestamp": _optional_str(
            _mapping_value(raw_message.get("Attributes"), "SentTimestamp")
        ),
        "body_summary": _body_summary(raw_message.get("Body")),
        "message_attribute_keys": sorted(_string_mapping(raw_message.get("MessageAttributes"))),
    }
    if parsed.get("ok"):
        row["event_id"] = parsed.get("event_id")
        row["transition_key"] = parsed.get("transition_key")
        row["incident_fingerprint"] = parsed.get("incident_fingerprint")
    else:
        row["parser_error"] = parsed.get("error", "parse failed")
    return row


def _body_summary(body: object) -> dict[str, Any]:
    payload = _json_object(body)
    if payload is None:
        return {"type": "text", "keys": []}
    summary: dict[str, Any] = {
        "type": _safe_envelope_type(payload),
        "keys": sorted(str(key) for key in payload),
    }
    inner_payload = _json_object(payload.get("Message"))
    if inner_payload is not None:
        summary["message_keys"] = sorted(str(key) for key in inner_payload)
    elif isinstance(payload.get("detail"), Mapping):
        summary["detail_keys"] = sorted(str(key) for key in payload["detail"])
    return summary


def _safe_envelope_type(payload: Mapping[str, Any]) -> str:
    raw_type = payload.get("Type") or payload.get("detail-type")
    if raw_type in {"Notification", "CloudWatch Alarm State Change"}:
        return str(raw_type)
    return "json"


def _json_object(value: object) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except Exception:
        return None
    if not isinstance(parsed, Mapping):
        return None
    return {str(key): item for key, item in parsed.items()}


def _mapping_value(value: object, key: str) -> object:
    if not isinstance(value, Mapping):
        return None
    return value.get(key)


def _int_mapping_value(value: object, key: str) -> int | None:
    raw = _mapping_value(value, key)
    if raw is None or raw == "":
        return None
    return int(str(raw))


def _parsed_alert_row(*, message_id: str, alert: ParsedCloudAlert) -> dict[str, Any]:
    normalized = _normalized_alert(alert)
    return {
        "ok": True,
        "message_id": message_id,
        "event_id": alert.event_id,
        "transition_key": alert.transition_key,
        "incident_fingerprint": alert.incident_fingerprint,
        "normalized_alert": normalized,
    }


def _normalized_alert(alert: ParsedCloudAlert) -> dict[str, Any]:
    payload = asdict(alert)
    payload.pop("raw", None)
    return payload


def _optional_str(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _string_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _safe_client_error(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            code = error.get("Code")
            if code:
                return str(code)
    return exc.__class__.__name__
