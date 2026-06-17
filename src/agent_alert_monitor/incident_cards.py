from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from agent_alert_monitor.alert import ParsedCloudAlert


@dataclass(frozen=True)
class IncidentCardContext:
    project_display_name: str
    project_slug: str
    environment: str
    source_name: str
    source_transport: str = "aws_sqs"


CLASSIFICATIONS = (
    "self-recovered/transient",
    "code-fix-likely",
    "infra-ops-needed",
    "human-decision-needed",
    "missing-access/tooling",
    "false-positive/noise",
)


def render_cloudwatch_incident_card(alert: ParsedCloudAlert, context: IncidentCardContext) -> str:
    """Render a deterministic Kanban incident card from a normalized cloud alert.

    The renderer intentionally consumes only ParsedCloudAlert fields. It must not inspect or
    serialize raw SNS/EventBridge/SQS envelopes because those may contain signatures, receipt
    handles, unsubscribe URLs, or provider-specific payloads that are not needed by workers.
    """

    title = (
        "# CloudWatch Alert Recovery"
        if alert.state.upper() == "OK"
        else "# CloudWatch Alert Incident"
    )
    lines: list[str] = [
        title,
        "",
        f"Project: {context.project_display_name} (`{context.project_slug}`)",
        f"Environment: {_value(context.environment)}",
        f"Source: {context.source_transport}/{context.source_name}",
        f"State: {_value(alert.state)}",
        f"Previous state: {_value(alert.previous_state)}",
        f"State changed at: {_value(alert.state_changed_at)}",
        f"Alarm: {_value(alert.alarm_name)}",
        f"Alarm ARN: {_value(alert.alarm_arn)}",
        f"Service: {_value(_metadata_str(alert, 'service'))}",
        f"Log group: {_value(_log_group(alert))}",
        f"Region: {_value(alert.region)}",
        f"Account: {_value(alert.account_id)}",
        f"Event id: {_value(alert.event_id)}",
        f"Transition key: {_value(alert.transition_key)}",
        f"Incident fingerprint: {_value(alert.incident_fingerprint)}",
        "",
    ]

    if alert.state.upper() == "OK":
        lines.extend(
            [
                "Recovery note: correlate this OK transition with an open incident using "
                "the incident fingerprint before closing anything.",
                "",
            ]
        )

    lines.extend(_state_reason_section(alert))
    lines.extend(_metric_section(alert))
    lines.extend(_links_section(alert))
    lines.extend(_commands_section(alert))
    lines.extend(_debugger_protocol_section())
    lines.extend(_debugger_output_contract_section())
    lines.extend(_coder_handoff_section())
    return "\n".join(lines).rstrip() + "\n"


def cloudwatch_alarm_url(*, region: str, alarm_name: str) -> str:
    encoded_alarm = quote(alarm_name, safe="")
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#alarmsV2:alarm/{encoded_alarm}"
    )


def cloudwatch_logs_url(*, region: str, log_group: str) -> str:
    encoded_log_group = quote(log_group, safe="").replace("%", "$25")
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#logsV2:log-groups/log-group/{encoded_log_group}"
    )


def _state_reason_section(alert: ParsedCloudAlert) -> list[str]:
    lines = ["## State reason", ""]
    lines.append(
        alert.reason.strip() if alert.reason else "No state reason provided by CloudWatch."
    )
    if alert.reason_data:
        lines.extend(
            [
                "",
                "Reason data:",
                "```json",
                json.dumps(alert.reason_data, indent=2, sort_keys=True),
                "```",
            ]
        )
    lines.append("")
    return lines


def _metric_section(alert: ParsedCloudAlert) -> list[str]:
    lines = [
        "## Metric / trigger",
        "",
        f"Namespace: {_value(alert.namespace)}",
        f"Metric: {_value(alert.metric_name)}",
        "Dimensions:",
    ]
    if alert.dimensions:
        lines.extend(f"- {key}={alert.dimensions[key]}" for key in sorted(alert.dimensions))
    else:
        lines.append("- unavailable")
    lines.append("")
    return lines


def _links_section(alert: ParsedCloudAlert) -> list[str]:
    alarm_link = _investigation_str(alert, "cloudwatch_alarm_url")
    if not alarm_link and alert.region and alert.alarm_name:
        alarm_link = cloudwatch_alarm_url(region=alert.region, alarm_name=alert.alarm_name)

    log_group = _log_group(alert)
    logs_link = None
    if alert.region and log_group:
        logs_link = cloudwatch_logs_url(region=alert.region, log_group=log_group)
    if not logs_link:
        logs_link = _investigation_str(alert, "cloudwatch_logs_url")

    lines = ["## CloudWatch links", ""]
    lines.append(f"- Alarm: {alarm_link}" if alarm_link else "- Alarm: unavailable")
    lines.append(f"- Logs: {logs_link}" if logs_link else "- Logs: unavailable")
    lines.append("")
    return lines


def _commands_section(alert: ParsedCloudAlert) -> list[str]:
    lines = ["## Investigation commands", "", "```bash"]
    if alert.region and alert.alarm_name:
        lines.append(
            "aws cloudwatch describe-alarms"
            f" --region {_shell(alert.region)}"
            f" --alarm-names {_shell(alert.alarm_name)}"
        )

    log_group = _log_group(alert)
    if alert.region and log_group:
        lines.append(
            "aws logs filter-log-events"
            f" --region {_shell(alert.region)}"
            f" --log-group-name {_shell(log_group)}"
            " --start-time \"$(python3 -c 'import time; print(int((time.time() - 1800) * 1000))')\""
            " --filter-pattern '%ERROR|error|FATAL|panic%'"
        )

    if alert.region and alert.namespace and alert.metric_name:
        metric_query = _metric_data_query(alert)
        lines.append(
            "aws cloudwatch get-metric-data"
            f" --region {_shell(alert.region)}"
            " --start-time $(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"
            " --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ)"
            f" --metric-data-queries {_shell(metric_query)}"
        )
    lines.extend(["```", ""])
    return lines


def _metric_data_query(alert: ParsedCloudAlert) -> str:
    dimensions = [
        {"Name": key, "Value": alert.dimensions[key]} for key in sorted(alert.dimensions)
    ]
    query = [
        {
            "Id": "alarm_metric",
            "MetricStat": {
                "Metric": {
                    "Namespace": alert.namespace,
                    "MetricName": alert.metric_name,
                    "Dimensions": dimensions,
                },
                "Period": _int_reason_data(alert, "period", default=60),
                "Stat": _str_reason_data(alert, "statistic", default="Sum"),
            },
            "ReturnData": True,
        }
    ]
    return json.dumps(query, separators=(",", ":"), sort_keys=True)


def _int_reason_data(alert: ParsedCloudAlert, key: str, *, default: int) -> int:
    value = alert.reason_data.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_reason_data(alert: ParsedCloudAlert, key: str, *, default: str) -> str:
    value = alert.reason_data.get(key)
    if value is None or value == "":
        return default
    return str(value)


def _debugger_protocol_section() -> list[str]:
    lines = [
        "## Required debugger protocol",
        "",
        "1. Acknowledge investigation in Telegram.",
        "2. Inspect current alarm state, recent log errors, metric trend, recent deploy context.",
        "3. Classify one of:",
    ]
    lines.extend(f"   - {classification}" for classification in CLASSIFICATIONS)
    lines.extend(
        [
            "4. If code-fix-likely, create a coder card with a bounded hypothesis and evidence.",
            "5. Never close/block silently; post final or blocked status first.",
            "",
        ]
    )
    return lines


def _debugger_output_contract_section() -> list[str]:
    contract: dict[str, Any] = {
        "type": "debugger_result",
        "incident_id": "inc_...",
        "classification": "code-fix-likely",
        "confidence": "medium",
        "evidence": [
            {
                "source": "cloudwatch_logs",
                "summary": "5 errors in payment processor after deploy",
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
    lines = [
        "## Debugger output contract",
        "",
        "Leave a structured comment or artifact on this incident card matching this JSON shape:",
        "",
        "```json",
        json.dumps(contract, indent=2, sort_keys=False),
        "```",
        "",
        "Allowed classifications:",
    ]
    lines.extend(f"- {classification}" for classification in CLASSIFICATIONS)
    lines.append("")
    return lines


def _coder_handoff_section() -> list[str]:
    return [
        "## Coder handoff rules",
        "",
        "Only create a coder card when debugger_result.requires_coder is true.",
        "Coder card must include:",
        "- Parent incident id and alarm name.",
        "- Service, log group, and suspected code area when known.",
        "- Evidence summary from debugger output.",
        "- Non-goals: do not change thresholds first; do not suppress alarms unless "
        "classified false-positive/noise; do not merge without review.",
        "- Acceptance: reproduce or explain failure path, add/adjust tests, open PR "
        "with canonical incident marker, post PR ref back to incident card.",
        "",
    ]


def _metadata_str(alert: ParsedCloudAlert, key: str) -> str | None:
    value = alert.metadata.get(key)
    if value is None or value == "":
        return None
    return str(value)


def _log_group(alert: ParsedCloudAlert) -> str | None:
    return _metadata_str(alert, "log_group") or _investigation_str(alert, "suggested_log_group")


def _investigation_str(alert: ParsedCloudAlert, key: str) -> str | None:
    value = alert.investigation.get(key)
    if value is None or value == "":
        return None
    return str(value)


def _value(value: object | None) -> str:
    if value is None or value == "":
        return "unavailable"
    return str(value)


def _shell(value: str) -> str:
    return shlex.quote(value)
