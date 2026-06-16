from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ParsedAlert:
    alarm_name: str
    service: str | None
    severity: str
    state: str
    region: str | None
    environment: str | None
    summary: str


@dataclass(frozen=True)
class SqsMessage:
    message_id: str
    receipt_handle: str | None
    body: dict[str, Any] | str
    attributes: dict[str, Any] = field(default_factory=dict)
    message_attributes: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def body_json(self) -> dict[str, Any]:
        if isinstance(self.body, dict):
            return self.body
        parsed = json.loads(self.body)
        if not isinstance(parsed, dict):
            raise ValueError("SQS message body must decode to a JSON object")
        return parsed


@dataclass(frozen=True)
class ParsedCloudAlert:
    source_type: str
    event_id: str
    project_slug: str
    account_id: str
    region: str
    alarm_arn: str | None
    alarm_name: str
    state: str
    previous_state: str | None
    state_changed_at: str
    transition_key: str = field(init=False)
    incident_fingerprint: str = field(init=False)
    reason: str | None = None
    reason_data: dict[str, Any] = field(default_factory=dict)
    namespace: str | None = None
    metric_name: str | None = None
    dimensions: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    investigation: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "transition_key", cloudwatch_transition_key(self))
        object.__setattr__(self, "incident_fingerprint", cloudwatch_incident_fingerprint(self))


_KEY_VALUE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*)=([^\s,;]+)")


def parse_alert_text(raw_text: str) -> ParsedAlert:
    text = " ".join(raw_text.strip().split())
    pairs = {k.lower(): v.strip("\"'") for k, v in _KEY_VALUE_RE.findall(text)}
    state_match = re.search(r"\b(ALARM|OK|INSUFFICIENT_DATA|RECOVERY|RESOLVED)\b", text, re.I)
    state = (pairs.get("state") or (state_match.group(1) if state_match else "ALARM")).upper()
    severity = (
        "critical" if re.search(r"\b(CRITICAL|P0|SEV[ -]?1|OUTAGE)\b", text, re.I) else "normal"
    )
    service = pairs.get("service") or pairs.get("svc")
    region = pairs.get("region") or pairs.get("aws_region")
    environment = pairs.get("env") or pairs.get("environment") or "prod"

    alarm_match = re.search(r"(?:ALARM|OK|RECOVERY|RESOLVED)[: ]+([A-Za-z0-9_.:/-]+)", text, re.I)
    alarm_name = (
        pairs.get("alarm")
        or pairs.get("alarm_name")
        or (alarm_match.group(1) if alarm_match else "unknown-alert")
    )
    alarm_name = alarm_name.strip("\"'")

    return ParsedAlert(
        alarm_name=alarm_name,
        service=service,
        severity=severity,
        state=state,
        region=region,
        environment=environment,
        summary=f"{alarm_name} {service or 'unknown-service'} {severity}",
    )


def fingerprint_alert(parsed: ParsedAlert) -> str:
    stable = "|".join(
        [
            parsed.alarm_name.lower(),
            (parsed.service or "").lower(),
            (parsed.region or "").lower(),
            (parsed.environment or "").lower(),
        ]
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def event_id_for_sqs_message(message: SqsMessage, *, source_type: str) -> str:
    body = message.body_json
    if source_type == "aws_sns_cloudwatch_alarm":
        topic_arn = _required_body_value(body, "TopicArn", source_type=source_type)
        message_id = _required_body_value(body, "MessageId", source_type=source_type)
        return f"sns:{topic_arn}:{message_id}"
    if source_type == "aws_eventbridge_cloudwatch_alarm":
        account = _required_body_value(body, "account", source_type=source_type)
        region = _required_body_value(body, "region", source_type=source_type)
        event_id = _required_body_value(body, "id", source_type=source_type)
        return f"eventbridge:{account}:{region}:{event_id}"
    raise ValueError(f"unsupported SQS alert source type: {source_type}")


def cloudwatch_transition_key(alert: ParsedCloudAlert) -> str:
    alarm_identity = alert.alarm_arn or alert.alarm_name
    return ":".join(
        [
            "cloudwatch-transition",
            alert.account_id,
            alert.region,
            alarm_identity,
            alert.state,
            alert.state_changed_at,
        ]
    )


def cloudwatch_incident_fingerprint(alert: ParsedCloudAlert) -> str:
    alarm_identity = alert.alarm_arn or alert.alarm_name
    return ":".join(["cloudwatch-alarm", alert.account_id, alert.region, alarm_identity])


def _required_body_value(body: dict[str, Any], key: str, *, source_type: str) -> str:
    value = body.get(key)
    if value is None or value == "":
        raise ValueError(f"missing {key} in {source_type} SQS message body")
    return str(value)
