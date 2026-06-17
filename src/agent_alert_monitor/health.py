from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import requests

from .config import AgentConfig, TelegramSinkConfig
from .ledger import AlertLedger
from .sqs_ingest import Boto3SqsClient, _safe_client_error, find_sqs_source


class StsClient(Protocol):
    def get_caller_identity(self) -> Mapping[str, Any]: ...


class Boto3StsClient:
    def __init__(self, *, region_name: str) -> None:
        try:
            import boto3  # type: ignore[import-not-found, import-untyped]
        except ImportError as exc:  # pragma: no cover - exercised only without optional runtime dep
            raise RuntimeError(
                "health command requires boto3; install agent-alert-monitor with AWS dependencies"
            ) from exc
        self._client = boto3.client("sts", region_name=region_name)

    def get_caller_identity(self) -> Mapping[str, Any]:
        return self._client.get_caller_identity()


def build_health_report(
    cfg: AgentConfig, *, source_name: str, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    env_map = os.environ if env is None else env
    source = find_sqs_source(cfg, source_name)
    checks: dict[str, Any] = {}

    checks["sqlite"] = _sqlite_status(cfg.runtime.ledger_path)
    checks["aws_identity"] = _aws_identity_status(source.region)
    _add_sqs_checks(checks, source)
    _add_hermes_checks(checks, cfg)
    checks["telegram_sink"] = _telegram_sink_status(cfg.project.telegram_sink, env=env_map)

    return {
        "ok": _checks_ok(checks),
        "project": cfg.project_slug,
        "source": source.name,
        "checks": checks,
    }


def _sqlite_status(ledger_path: Path) -> str:
    try:
        ledger = AlertLedger(ledger_path)
        with ledger.connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"failed: {_safe_client_error(exc)}"
    return "ok"


def _aws_identity_status(region: str) -> str:
    try:
        Boto3StsClient(region_name=region).get_caller_identity()
    except Exception as exc:
        return f"failed: {_safe_client_error(exc)}"
    return "ok"


def _add_sqs_checks(checks: dict[str, Any], source: Any) -> None:
    try:
        client = Boto3SqsClient(region_name=source.region)
        attrs = client.get_queue_attributes(
            QueueUrl=source.queue_url,
            AttributeNames=[
                "QueueArn",
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        ).get("Attributes", {})
    except Exception as exc:
        checks["sqs_queue_access"] = f"failed: {_safe_client_error(exc)}"
        checks["sqs_oldest_message_age_seconds"] = "skipped"
        checks["sqs_approx_visible"] = "skipped"
        checks["sqs_approx_inflight"] = "skipped"
        checks["sqs_dlq_visible"] = "skipped"
        return

    if source.queue_arn and attrs.get("QueueArn") != source.queue_arn:
        checks["sqs_queue_access"] = "failed: arn_mismatch"
    else:
        checks["sqs_queue_access"] = "ok"
    checks["sqs_oldest_message_age_seconds"] = "not_available: cloudwatch_metric"
    checks["sqs_approx_visible"] = _int_attr(attrs, "ApproximateNumberOfMessages")
    checks["sqs_approx_inflight"] = _int_attr(attrs, "ApproximateNumberOfMessagesNotVisible")

    if not source.dlq_queue_url:
        checks["sqs_dlq_visible"] = "failed: not_configured"
        return
    try:
        dlq_attrs = client.get_queue_attributes(
            QueueUrl=source.dlq_queue_url,
            AttributeNames=["QueueArn", "ApproximateNumberOfMessages"],
        ).get("Attributes", {})
        if source.dlq_queue_arn and dlq_attrs.get("QueueArn") != source.dlq_queue_arn:
            checks["sqs_dlq_visible"] = "failed: arn_mismatch"
        else:
            checks["sqs_dlq_visible"] = _int_attr(dlq_attrs, "ApproximateNumberOfMessages")
    except Exception as exc:
        checks["sqs_dlq_visible"] = f"failed: {_safe_client_error(exc)}"


def _add_hermes_checks(checks: dict[str, Any], cfg: AgentConfig) -> None:
    hermes_bin = shutil.which("hermes")
    if not hermes_bin:
        checks["hermes_binary"] = "failed"
        checks["hermes_profile"] = "skipped"
        checks["kanban_board"] = "skipped"
        return

    checks["hermes_binary"] = "ok"
    profile = cfg.hermes.coordinator_profile
    if _run_lists_name([hermes_bin, "profile", "list"], profile):
        checks["hermes_profile"] = "ok"
    else:
        checks["hermes_profile"] = "failed"

    board = cfg.hermes.kanban_board
    if not board:
        checks["kanban_board"] = "not_configured"
    elif checks["hermes_profile"] != "ok":
        checks["kanban_board"] = "skipped"
    elif _run_lists_name([hermes_bin, "-p", profile, "kanban", "boards", "list"], board):
        checks["kanban_board"] = "ok"
    else:
        checks["kanban_board"] = "failed"


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


def _telegram_sink_status(sink: TelegramSinkConfig | None, *, env: Mapping[str, str]) -> str:
    if sink is None:
        return "not_configured"
    if sink.bot_token_env and not env.get(sink.bot_token_env) and sink.bot_token in {None, "", "0"}:
        return "failed: missing_env"
    if sink.chat_id_env and not env.get(sink.chat_id_env) and sink.chat_id in {"", "0"}:
        return "failed: missing_env"
    if not sink.bot_token or not sink.chat_id:
        return "failed"
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{sink.bot_token}/getChat",
            params={"chat_id": sink.chat_id},
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("ok") is not True:
            return "failed"
    except Exception as exc:
        return f"failed: {_safe_client_error(exc)}"
    return "ok"


def _int_attr(attrs: object, key: str) -> int:
    if not isinstance(attrs, Mapping):
        return 0
    return int(attrs.get(key, 0) or 0)


def _checks_ok(checks: Mapping[str, Any]) -> bool:
    for key, value in checks.items():
        if isinstance(value, str) and (value.startswith("failed") or value == "not_configured"):
            return False
        if key == "sqs_dlq_visible" and isinstance(value, int) and value > 0:
            return False
    return True
