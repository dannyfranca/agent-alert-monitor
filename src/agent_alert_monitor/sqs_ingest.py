from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from .alert import AlertEnvelopeParser, CloudAlertParseError, ParsedCloudAlert, SqsMessage
from .cloud_parsers import AwsEventBridgeCloudWatchAlarmParser, AwsSnsCloudWatchAlarmParser
from .config import AgentConfig, AwsSqsSourceConfig
from .incident_cards import IncidentCardContext, render_cloudwatch_incident_card
from .kanban import KanbanCardRequest, KanbanClient
from .ledger import AlertLedger, CloudAlertProcessResult
from .telegram_ingest import send_telegram_message

REQUIRED_SIDE_EFFECTS = "required_side_effects"


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    checks: dict[str, str]


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

    def change_message_visibility(self, **kwargs: Any) -> Mapping[str, Any]:
        return self._client.change_message_visibility(**kwargs)


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
        raise ValueError("live sqs-ingest is not implemented yet; use sqs-listen")
    effective_max_messages = _effective_max_messages(
        max_messages if max_messages is not None else source.max_messages
    )
    sqs_client = client or Boto3SqsClient(region_name=source.region)
    raw_messages = _receive_messages(source, effective_max_messages, sqs_client)

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


def listen_for_sqs_messages(
    cfg: AgentConfig,
    *,
    source_name: str,
    ledger: AlertLedger | None = None,
    kanban_client: KanbanClient | None = None,
    client: SqsClient | None = None,
    preflight: Callable[[], PreflightResult] | None = None,
    status_sender: Callable[[AgentConfig, str], None] | None = None,
    once: bool = False,
    sleep_seconds: float = 5.0,
) -> dict[str, Any]:
    source = find_sqs_source(cfg, source_name)
    sqs_client = client or Boto3SqsClient(region_name=source.region)
    alert_ledger = ledger or AlertLedger(cfg.runtime.ledger_path)
    sender = status_sender or _send_telegram_status_if_configured
    while True:
        result = _listen_once(
            cfg,
            source=source,
            ledger=alert_ledger,
            kanban_client=kanban_client,
            client=sqs_client,
            preflight=preflight,
            status_sender=sender,
        )
        if once:
            return result
        if result["preflight_ok"] is False:
            time.sleep(sleep_seconds)
            continue
        if result["messages_received"] == 0:
            time.sleep(sleep_seconds)


def _listen_once(
    cfg: AgentConfig,
    *,
    source: AwsSqsSourceConfig,
    ledger: AlertLedger,
    kanban_client: KanbanClient | None,
    client: SqsClient,
    preflight: Callable[[], PreflightResult] | None,
    status_sender: Callable[[AgentConfig, str], None],
) -> dict[str, Any]:
    preflight_result = (
        preflight() if preflight else run_local_preflight(cfg, source, ledger, client)
    )
    if not preflight_result.ok:
        return {
            "project": cfg.project_slug,
            "source": source.name,
            "preflight_ok": False,
            "checks": preflight_result.checks,
            "messages_received": 0,
            "messages": [],
        }

    raw_messages = _receive_messages(source, source.max_messages, client)
    parser = _parser_for_source(source, project_slug=cfg.project_slug)
    rows = [
        _process_live_sqs_message(
            cfg,
            source=source,
            raw_message=raw_message,
            parser=parser,
            ledger=ledger,
            kanban_client=kanban_client,
            client=client,
            status_sender=status_sender,
        )
        for raw_message in raw_messages
    ]
    return {
        "project": cfg.project_slug,
        "source": source.name,
        "preflight_ok": True,
        "checks": preflight_result.checks,
        "messages_received": len(rows),
        "messages": rows,
    }


def run_local_preflight(
    cfg: AgentConfig,
    source: AwsSqsSourceConfig,
    ledger: AlertLedger,
    client: SqsClient,
) -> PreflightResult:
    checks: dict[str, str] = {}
    try:
        with ledger.connect() as conn:
            conn.execute("PRAGMA user_version").fetchone()
        checks["sqlite"] = "ok"
    except Exception:
        checks["sqlite"] = "failed"

    hermes_bin = shutil.which("hermes")
    checks["hermes_binary"] = "ok" if hermes_bin else "failed"
    checks["hermes_profile"] = _hermes_profile_preflight_status(
        hermes_bin, cfg.hermes.coordinator_profile
    )
    checks["kanban_board"] = _kanban_board_preflight_status(
        hermes_bin,
        cfg.hermes.coordinator_profile,
        cfg.hermes.kanban_board,
        profile_ok=checks["hermes_profile"] == "ok",
    )

    try:
        attrs = client.get_queue_attributes(  # type: ignore[attr-defined]
            QueueUrl=source.queue_url,
            AttributeNames=[
                "QueueArn",
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        ).get("Attributes", {})
        if source.queue_arn and _mapping_value(attrs, "QueueArn") != source.queue_arn:
            checks["sqs_queue_access"] = "failed: arn_mismatch"
        else:
            checks["sqs_queue_access"] = "ok"
    except Exception:
        checks["sqs_queue_access"] = "failed"

    try:
        import boto3  # type: ignore[import-not-found, import-untyped]

        boto3.client("sts", region_name=source.region).get_caller_identity()
        checks["aws_identity"] = "ok"
    except Exception:
        checks["aws_identity"] = "failed"

    if cfg.project.telegram_sink is None:
        checks["telegram_sink"] = "not_configured"
    elif cfg.telegram.bot_token and cfg.telegram.alert_chat_id:
        checks["telegram_sink"] = "configured"
    else:
        checks["telegram_sink"] = "failed"

    required_checks = (
        "sqlite",
        "hermes_binary",
        "hermes_profile",
        "kanban_board",
        "aws_identity",
        "sqs_queue_access",
    )
    failed_required = {key for key in required_checks if checks.get(key) != "ok"}
    if cfg.project.telegram_sink is not None and checks.get("telegram_sink") == "failed":
        checks["telegram_sink"] = "disabled"
    return PreflightResult(ok=not failed_required, checks=checks)


def _hermes_profile_preflight_status(hermes_bin: str | None, profile: str) -> str:
    if not hermes_bin:
        return "failed"
    return "ok" if _run_lists_name([hermes_bin, "profile", "list"], profile) else "failed"


def _kanban_board_preflight_status(
    hermes_bin: str | None,
    profile: str,
    board: str | None,
    *,
    profile_ok: bool,
) -> str:
    if not hermes_bin:
        return "failed"
    if not board:
        return "failed"
    if not profile_ok:
        return "failed"
    if _run_lists_name([hermes_bin, "-p", profile, "kanban", "boards", "list"], board):
        return "ok"
    return "failed"


def _run_lists_name(command: list[str], expected_name: str) -> bool:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    except Exception:
        return False
    if result.returncode != 0:
        return False
    return any(_listed_name_matches(line, expected_name) for line in result.stdout.splitlines())


def _listed_name_matches(line: str, expected_name: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    first_column = stripped.split()[0]
    return stripped == expected_name or first_column == expected_name


def _receive_messages(
    source: AwsSqsSourceConfig,
    max_messages: int,
    sqs_client: SqsClient,
) -> list[Mapping[str, Any]]:
    effective_max_messages = _effective_max_messages(max_messages)
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
    return [message for message in raw_messages if isinstance(message, Mapping)]


def _process_live_sqs_message(
    cfg: AgentConfig,
    *,
    source: AwsSqsSourceConfig,
    raw_message: Mapping[str, Any],
    parser: AlertEnvelopeParser,
    ledger: AlertLedger,
    kanban_client: KanbanClient | None,
    client: SqsClient,
    status_sender: Callable[[AgentConfig, str], None],
) -> dict[str, Any]:
    sqs_message = _sqs_message_from_raw(raw_message)
    try:
        alert = parser.parse(sqs_message)
    except CloudAlertParseError as exc:
        failure = ledger.record_alert_parse_failure(
            source.name, cfg.project_slug, sqs_message, str(exc)
        )
        return {
            "ok": False,
            "message_id": sqs_message.message_id,
            "event_id": failure.event_id,
            "action": "parse_failed",
            "deleted": False,
            "error": str(exc),
        }

    process_result = ledger.process_cloud_alert(source.name, sqs_message, alert)
    row: dict[str, Any] = {
        "ok": True,
        "message_id": sqs_message.message_id,
        "event_id": alert.event_id,
        "transition_key": alert.transition_key,
        "incident_fingerprint": alert.incident_fingerprint,
        "action": process_result.action,
        "incident_id": process_result.incident_id,
        "deleted": False,
    }

    if ledger.alert_side_effect_succeeded(alert.event_id, REQUIRED_SIDE_EFFECTS):
        try:
            _delete_message(client, source, sqs_message)
        except Exception as exc:
            row["error"] = _safe_client_error(exc)
            return row
        row["deleted"] = True
        return row

    try:
        _perform_required_side_effects(
            cfg,
            source=source,
            alert=alert,
            process_result=process_result,
            ledger=ledger,
            kanban_client=kanban_client,
            status_sender=status_sender,
        )
    except Exception as exc:
        row["error"] = _safe_client_error(exc)
        return row

    ledger.record_alert_side_effect(
        alert.event_id,
        REQUIRED_SIDE_EFFECTS,
        "succeeded",
        {"action": process_result.action, "incident_id": process_result.incident_id},
    )
    try:
        _delete_message(client, source, sqs_message)
    except Exception as exc:
        row["error"] = _safe_client_error(exc)
        return row
    row["deleted"] = True
    return row


def _perform_required_side_effects(
    cfg: AgentConfig,
    *,
    source: AwsSqsSourceConfig,
    alert: ParsedCloudAlert,
    process_result: CloudAlertProcessResult,
    ledger: AlertLedger,
    kanban_client: KanbanClient | None,
    status_sender: Callable[[AgentConfig, str], None],
) -> None:
    if process_result.action == "opened":
        if process_result.incident_id is None:
            raise RuntimeError("opened cloud alert did not return an incident id")
        task_id = _ensure_kanban_incident_card(
            cfg,
            source=source,
            alert=alert,
            incident_id=process_result.incident_id,
            ledger=ledger,
            kanban_client=kanban_client,
        )
        _send_status(
            cfg,
            status_sender,
            f"Alert monitor: opened incident {task_id} for {alert.alarm_name}",
        )
        return

    if process_result.action in {"correlated", "duplicate_transition"}:
        incident = (
            ledger.get_cloud_incident_by_id(process_result.incident_id)
            if process_result.incident_id
            else ledger.get_cloud_incident_for_transition(
                alert.transition_key, alert.project_slug, alert.incident_fingerprint
            )
        )
        if incident is None:
            if process_result.action == "duplicate_transition":
                return
            raise RuntimeError("correlated cloud alert has no incident row")
        correlated_task_id = _ensure_kanban_incident_card(
            cfg,
            source=source,
            alert=alert,
            incident_id=incident.incident_task_id,
            ledger=ledger,
            kanban_client=kanban_client,
            card_alert=ledger.get_cloud_alert_for_event(incident.first_event_id),
        )
        if kanban_client is None:
            raise RuntimeError("live sqs-listen requires a Kanban client")
        kanban_client.comment(
            correlated_task_id,
            (
                f"Correlated {alert.state} transition `{alert.transition_key}` "
                f"for `{alert.alarm_name}`."
            ),
        )
        _send_status(
            cfg,
            status_sender,
            f"Alert monitor: correlated {alert.state} for {alert.alarm_name}",
        )
        return

    if process_result.action == "resolved":
        incident = (
            ledger.get_cloud_incident_by_id(process_result.incident_id)
            if process_result.incident_id
            else ledger.get_cloud_incident_for_event(
                alert.event_id, alert.project_slug, alert.incident_fingerprint
            )
        )
        if incident is None:
            raise RuntimeError("resolved cloud alert has no incident row")
        resolved_task_id = ledger.cloud_incident_kanban_task_id(incident.incident_task_id)
        if not resolved_task_id:
            raise RuntimeError("resolved cloud alert has no Kanban task")
        if kanban_client is None:
            raise RuntimeError("live sqs-listen requires a Kanban client")
        kanban_client.comment(
            resolved_task_id,
            (
                f"Recovered with transition `{alert.transition_key}`; "
                f"incident fingerprint `{alert.incident_fingerprint}`."
            ),
        )
        _send_status(cfg, status_sender, f"Alert monitor: recovered {alert.alarm_name}")
        return

    if process_result.action == "duplicate_event":
        incident = ledger.get_cloud_incident_for_event(
            alert.event_id, alert.project_slug, alert.incident_fingerprint
        )
        if incident is None:
            return
        if alert.state == "ALARM":
            duplicate_alarm_task_id = _ensure_kanban_incident_card(
                cfg,
                source=source,
                alert=alert,
                incident_id=incident.incident_task_id,
                ledger=ledger,
                kanban_client=kanban_client,
                card_alert=ledger.get_cloud_alert_for_event(incident.first_event_id),
            )
            if incident.first_event_id != alert.event_id:
                if kanban_client is None:
                    raise RuntimeError("live sqs-listen requires a Kanban client")
                kanban_client.comment(
                    duplicate_alarm_task_id,
                    (
                        f"Correlated retried ALARM event `{alert.event_id}`; "
                        f"transition `{alert.transition_key}` is already in the ledger."
                    ),
                )
        elif alert.state in {"OK", "RECOVERY", "RESOLVED"}:
            duplicate_recovery_task_id = ledger.cloud_incident_kanban_task_id(
                incident.incident_task_id
            )
            if not duplicate_recovery_task_id:
                raise RuntimeError("duplicate recovery cloud alert has no Kanban task")
            if kanban_client is None:
                raise RuntimeError("live sqs-listen requires a Kanban client")
            kanban_client.comment(
                duplicate_recovery_task_id,
                (
                    f"Recovered with retried duplicate event `{alert.event_id}`; "
                    f"transition `{alert.transition_key}` is already in the ledger."
                ),
            )
        return

    # unmatched recoveries, stale transitions, and observed/noise states have no required
    # external side effect for deletion safety; the durable ledger record is sufficient.


def _ensure_kanban_incident_card(
    cfg: AgentConfig,
    *,
    source: AwsSqsSourceConfig,
    alert: ParsedCloudAlert,
    incident_id: str,
    ledger: AlertLedger,
    kanban_client: KanbanClient | None,
    card_alert: ParsedCloudAlert | None = None,
) -> str:
    existing_task_id = ledger.cloud_incident_kanban_task_id(incident_id)
    if existing_task_id:
        return existing_task_id
    if kanban_client is None:
        raise RuntimeError("live sqs-listen requires a Kanban client")
    render_alert = card_alert or alert
    context = IncidentCardContext(
        project_display_name=cfg.project_display_name,
        project_slug=cfg.project_slug,
        environment=cfg.project.environment or "unknown",
        source_name=source.name,
    )
    body = render_cloudwatch_incident_card(render_alert, context)
    priority = (
        cfg.kanban.critical_priority
        if render_alert.state == "ALARM"
        else cfg.kanban.default_priority
    )
    title = f"{cfg.project_display_name} CloudWatch alert: {render_alert.alarm_name}"
    task_id = kanban_client.create_incident(
        KanbanCardRequest(
            title=title,
            assignee=cfg.kanban.incident_assignee,
            body=body,
            priority=priority,
            tenant=cfg.kanban.tenant,
            idempotency_key=f"agent-alert-monitor:{incident_id}",
        )
    )
    ledger.attach_cloud_incident_kanban_task(incident_id, task_id)
    return task_id


def _send_status(
    cfg: AgentConfig,
    status_sender: Callable[[AgentConfig, str], None],
    text: str,
) -> None:
    if cfg.project.telegram_sink is None:
        return
    try:
        status_sender(cfg, text)
    except Exception:
        # Telegram is an operator-visible status sink, not the source of truth for
        # live SQS intake. Required Kanban/ledger side effects still control safe
        # DeleteMessage behavior.
        return


def _send_telegram_status_if_configured(cfg: AgentConfig, text: str) -> None:
    if cfg.project.telegram_sink is None:
        return
    send_telegram_message(cfg, text)


def _delete_message(client: SqsClient, source: AwsSqsSourceConfig, message: SqsMessage) -> None:
    if not message.receipt_handle:
        raise RuntimeError("cannot delete SQS message without receipt handle")
    client.delete_message(QueueUrl=source.queue_url, ReceiptHandle=message.receipt_handle)


def _sqs_message_from_raw(raw_message: Mapping[str, Any]) -> SqsMessage:
    message_id = str(raw_message.get("MessageId") or "")
    return SqsMessage(
        message_id=message_id,
        receipt_handle=_optional_str(raw_message.get("ReceiptHandle")),
        body=raw_message.get("Body", ""),
        attributes=_string_mapping(raw_message.get("Attributes")),
        message_attributes=_string_mapping(raw_message.get("MessageAttributes")),
        raw=dict(raw_message),
    )


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
    sqs_message = _sqs_message_from_raw(raw_message)
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
