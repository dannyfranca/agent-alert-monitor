from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_alert_monitor.cli import main


class FakeSqsOpsClient:
    def __init__(self, *, region_name: str) -> None:
        self.region_name = region_name
        self.attribute_calls: list[dict[str, Any]] = []
        self.receive_calls: list[dict[str, Any]] = []

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.attribute_calls.append(kwargs)
        queue_url = kwargs["QueueUrl"]
        if queue_url.endswith("/intake"):
            return {
                "Attributes": {
                    "QueueArn": "arn:aws:sqs:sa-east-1:123456789012:agent-alert-monitor-intake",
                    "ApproximateNumberOfMessages": "2",
                    "ApproximateNumberOfMessagesNotVisible": "1",
                }
            }
        if queue_url.endswith("/dlq"):
            return {
                "Attributes": {
                    "QueueArn": "arn:aws:sqs:sa-east-1:123456789012:agent-alert-monitor-dlq",
                    "ApproximateNumberOfMessages": "0",
                }
            }
        raise AssertionError(f"unexpected queue url: {queue_url}")

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.receive_calls.append(kwargs)
        return {
            "Messages": [
                {
                    "MessageId": "dlq-message-1",
                    "ReceiptHandle": "receipt-secret-should-not-print",
                    "Attributes": {
                        "ApproximateReceiveCount": "5",
                        "SentTimestamp": "1781650000000",
                    },
                    "MessageAttributes": {
                        "Authorization": {"StringValue": "Bearer secret-token"},
                        "AlarmType": {"StringValue": "cloudwatch"},
                    },
                    "Body": json.dumps(
                        {
                            "Type": "Notification",
                            "MessageId": "sns-dlq-secret-id",
                            "Message": json.dumps(
                                {
                                    "AlarmName": "example-alarm",
                                    "SecretAccessKey": "super-secret-value",
                                }
                            ),
                        }
                    ),
                }
            ]
        }


class CustomReceiveSqsClient(FakeSqsOpsClient):
    def __init__(self, *, region_name: str, response: dict[str, Any]) -> None:
        super().__init__(region_name=region_name)
        self.response = response

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.receive_calls.append(kwargs)
        return self.response


class NonEmptyDlqSqsClient(FakeSqsOpsClient):
    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        result = super().get_queue_attributes(**kwargs)
        if kwargs["QueueUrl"].endswith("/dlq"):
            result["Attributes"]["ApproximateNumberOfMessages"] = "2"
        return result


class FailingSqsOpsClient:
    def __init__(self, *, region_name: str) -> None:
        self.region_name = region_name

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDenied for https://sqs.example/secret-account/intake")


class RaisingSqsConstructor:
    def __init__(self, *, region_name: str) -> None:
        raise RuntimeError("boto setup leaked-secret")


class FakeStsClient:
    def __init__(self, *, region_name: str) -> None:
        self.region_name = region_name

    def get_caller_identity(self) -> dict[str, str]:
        return {
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:user/agent-alert-monitor",
        }


class FailingStsClient:
    def __init__(self, *, region_name: str) -> None:
        self.region_name = region_name

    def get_caller_identity(self) -> dict[str, str]:
        raise RuntimeError("secret AWS token expired")


class FakeTelegramResponse:
    def __init__(self, *, ok: bool = True) -> None:
        self._ok = ok

    def raise_for_status(self) -> None:
        if not self._ok:
            raise RuntimeError("telegram secret token rejected")

    def json(self) -> dict[str, Any]:
        return {"ok": self._ok, "result": {"id": -100111}}


def _write_ops_config(tmp_path: Path) -> Path:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: {tmp_path / "state"}
projects:
  - slug: ticketdovale
    display_name: TicketDoVale
    sources:
      - name: ticketdovale-prod-alerts
        type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        queue_arn_env: TICKETDOVALE_AGENT_ALERT_QUEUE_ARN
        dlq_queue_url_env: TICKETDOVALE_AGENT_ALERT_DLQ_URL
        dlq_queue_arn_env: TICKETDOVALE_AGENT_ALERT_DLQ_ARN
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    sinks:
      - name: ticketdovale-telegram-status
        type: telegram
        bot_token_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN
        chat_id_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID
    hermes:
      coordinator_profile: alert-coordinator
      kanban_board: ticketdovale-incidents
    kanban:
      incident_assignee: debugger
""".strip(),
        encoding="utf-8",
    )
    return config_file


def _env() -> dict[str, str]:
    return {
        "TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/intake",
        "TICKETDOVALE_AGENT_ALERT_QUEUE_ARN": (
            "arn:aws:sqs:sa-east-1:123456789012:agent-alert-monitor-intake"
        ),
        "TICKETDOVALE_AGENT_ALERT_DLQ_URL": "https://sqs.example/dlq",
        "TICKETDOVALE_AGENT_ALERT_DLQ_ARN": (
            "arn:aws:sqs:sa-east-1:123456789012:agent-alert-monitor-dlq"
        ),
        "ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN": "telegram-secret-token",
        "ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID": "-100111",
    }


def test_health_json_reports_successful_operational_checks(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_ops_config(tmp_path)
    fake_sqs = FakeSqsOpsClient(region_name="sa-east-1")
    monkeypatch.setattr(
        "agent_alert_monitor.health.Boto3SqsClient", lambda *, region_name: fake_sqs
    )
    monkeypatch.setattr("agent_alert_monitor.health.Boto3StsClient", FakeStsClient)
    monkeypatch.setattr(
        "agent_alert_monitor.health.shutil.which", lambda name: "/home/agent/.local/bin/hermes"
    )
    monkeypatch.setattr(
        "agent_alert_monitor.health.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="alert-coordinator\nticketdovale-incidents\n", stderr=""
        ),
    )
    monkeypatch.setattr(
        "agent_alert_monitor.health.requests.get", lambda *args, **kwargs: FakeTelegramResponse()
    )

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=_env(),
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["source"] == "ticketdovale-prod-alerts"
    assert payload["checks"] == {
        "sqlite": "ok",
        "aws_identity": "ok",
        "sqs_queue_access": "ok",
        "sqs_oldest_message_age_seconds": "not_available: cloudwatch_metric",
        "sqs_approx_visible": 2,
        "sqs_approx_inflight": 1,
        "sqs_dlq_visible": 0,
        "hermes_binary": "ok",
        "hermes_profile": "ok",
        "kanban_board": "ok",
        "telegram_sink": "ok",
    }
    assert fake_sqs.attribute_calls == [
        {
            "QueueUrl": "https://sqs.example/intake",
            "AttributeNames": [
                "QueueArn",
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        },
        {
            "QueueUrl": "https://sqs.example/dlq",
            "AttributeNames": ["QueueArn", "ApproximateNumberOfMessages"],
        },
    ]


def test_health_returns_json_for_missing_source_queue_env(tmp_path: Path, capsys) -> None:
    config_file = _write_ops_config(tmp_path)
    env = {
        key: value for key, value in _env().items() if key != "TICKETDOVALE_AGENT_ALERT_QUEUE_URL"
    }

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=env,
    )

    assert code == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "ok": False,
        "source": "ticketdovale-prod-alerts",
        "checks": {"config": "failed: ValueError"},
    }
    assert "TICKETDOVALE_AGENT_ALERT_QUEUE_URL" not in output


def test_health_fails_for_queue_arn_mismatch(tmp_path: Path, capsys, monkeypatch) -> None:
    config_file = _write_ops_config(tmp_path)
    fake_sqs = FakeSqsOpsClient(region_name="sa-east-1")
    monkeypatch.setattr(
        "agent_alert_monitor.health.Boto3SqsClient", lambda *, region_name: fake_sqs
    )
    monkeypatch.setattr("agent_alert_monitor.health.Boto3StsClient", FakeStsClient)
    monkeypatch.setattr("agent_alert_monitor.health.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "agent_alert_monitor.health.requests.get", lambda *args, **kwargs: FakeTelegramResponse()
    )
    env = _env() | {
        "TICKETDOVALE_AGENT_ALERT_QUEUE_ARN": "arn:aws:sqs:sa-east-1:123456789012:wrong"
    }

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=env,
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["sqs_queue_access"] == "failed: arn_mismatch"


def test_health_fails_when_dlq_url_is_missing(tmp_path: Path, capsys, monkeypatch) -> None:
    config_file = _write_ops_config(tmp_path)
    fake_sqs = FakeSqsOpsClient(region_name="sa-east-1")
    monkeypatch.setattr(
        "agent_alert_monitor.health.Boto3SqsClient", lambda *, region_name: fake_sqs
    )
    monkeypatch.setattr("agent_alert_monitor.health.Boto3StsClient", FakeStsClient)
    monkeypatch.setattr("agent_alert_monitor.health.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "agent_alert_monitor.health.requests.get", lambda *args, **kwargs: FakeTelegramResponse()
    )
    env = {key: value for key, value in _env().items() if key != "TICKETDOVALE_AGENT_ALERT_DLQ_URL"}

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=env,
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["sqs_dlq_visible"] == "failed: not_configured"


def test_health_fails_when_dlq_has_visible_messages(tmp_path: Path, capsys, monkeypatch) -> None:
    config_file = _write_ops_config(tmp_path)
    fake_sqs = NonEmptyDlqSqsClient(region_name="sa-east-1")
    monkeypatch.setattr(
        "agent_alert_monitor.health.Boto3SqsClient", lambda *, region_name: fake_sqs
    )
    monkeypatch.setattr("agent_alert_monitor.health.Boto3StsClient", FakeStsClient)
    monkeypatch.setattr("agent_alert_monitor.health.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "agent_alert_monitor.health.requests.get", lambda *args, **kwargs: FakeTelegramResponse()
    )

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=_env(),
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["sqs_dlq_visible"] == 2


def test_health_fails_when_telegram_sink_env_is_missing(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_ops_config(tmp_path)
    fake_sqs = FakeSqsOpsClient(region_name="sa-east-1")
    monkeypatch.setattr(
        "agent_alert_monitor.health.Boto3SqsClient", lambda *, region_name: fake_sqs
    )
    monkeypatch.setattr("agent_alert_monitor.health.Boto3StsClient", FakeStsClient)
    monkeypatch.setattr("agent_alert_monitor.health.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "agent_alert_monitor.health.requests.get", lambda *args, **kwargs: FakeTelegramResponse()
    )
    env = {
        key: value
        for key, value in _env().items()
        if key != "ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN"
    }

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=env,
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["telegram_sink"] == "failed: missing_env"


def test_health_json_sanitizes_failures_and_returns_nonzero(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_ops_config(tmp_path)
    monkeypatch.setattr("agent_alert_monitor.health.Boto3SqsClient", FailingSqsOpsClient)
    monkeypatch.setattr("agent_alert_monitor.health.Boto3StsClient", FailingStsClient)
    monkeypatch.setattr("agent_alert_monitor.health.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "agent_alert_monitor.health.requests.get",
        lambda *args, **kwargs: FakeTelegramResponse(ok=False),
    )

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=_env(),
    )

    assert code == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["ok"] is False
    assert payload["checks"]["sqlite"] == "ok"
    assert payload["checks"]["aws_identity"] == "failed: RuntimeError"
    assert payload["checks"]["sqs_queue_access"] == "failed: RuntimeError"
    assert payload["checks"]["hermes_binary"] == "failed"
    assert payload["checks"]["hermes_profile"] == "skipped"
    assert payload["checks"]["kanban_board"] == "skipped"
    assert payload["checks"]["telegram_sink"] == "failed: RuntimeError"
    assert "secret" not in output.lower()
    assert "https://sqs.example" not in output
    assert "telegram-secret-token" not in output


def test_health_json_reports_sqs_constructor_failures(tmp_path: Path, capsys, monkeypatch) -> None:
    config_file = _write_ops_config(tmp_path)
    monkeypatch.setattr("agent_alert_monitor.health.Boto3SqsClient", RaisingSqsConstructor)
    monkeypatch.setattr("agent_alert_monitor.health.Boto3StsClient", FakeStsClient)
    monkeypatch.setattr("agent_alert_monitor.health.shutil.which", lambda name: None)
    monkeypatch.setattr(
        "agent_alert_monitor.health.requests.get", lambda *args, **kwargs: FakeTelegramResponse()
    )

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=_env(),
    )

    assert code == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["checks"]["sqs_queue_access"] == "failed: RuntimeError"
    assert "leaked-secret" not in output


def test_health_requires_exact_hermes_profile_and_board_names(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_ops_config(tmp_path)
    fake_sqs = FakeSqsOpsClient(region_name="sa-east-1")
    monkeypatch.setattr(
        "agent_alert_monitor.health.Boto3SqsClient", lambda *, region_name: fake_sqs
    )
    monkeypatch.setattr("agent_alert_monitor.health.Boto3StsClient", FakeStsClient)
    monkeypatch.setattr("agent_alert_monitor.health.shutil.which", lambda name: "/usr/bin/hermes")
    monkeypatch.setattr(
        "agent_alert_monitor.health.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="alert-coordinator-old\nticketdovale-incidents-old\n", stderr=""
        ),
    )
    monkeypatch.setattr(
        "agent_alert_monitor.health.requests.get", lambda *args, **kwargs: FakeTelegramResponse()
    )

    code = main(
        ["--config", str(config_file), "health", "--source", "ticketdovale-prod-alerts", "--json"],
        env=_env(),
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"]["hermes_profile"] == "failed"
    assert payload["checks"]["kanban_board"] == "skipped"


def test_dlq_inspect_prints_sanitized_payload_summary_and_parser_error(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_ops_config(tmp_path)
    fake_sqs = FakeSqsOpsClient(region_name="sa-east-1")
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient", lambda *, region_name: fake_sqs
    )

    code = main(
        [
            "--config",
            str(config_file),
            "dlq-inspect",
            "--source",
            "ticketdovale-prod-alerts",
            "--max-messages",
            "1",
        ],
        env=_env(),
    )

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["source"] == "ticketdovale-prod-alerts"
    assert payload["dlq_url_env"] == "TICKETDOVALE_AGENT_ALERT_DLQ_URL"
    assert payload["messages_received"] == 1
    assert payload["messages"][0]["ok"] is False
    assert payload["messages"][0]["message_id"] == "dlq-message-1"
    assert payload["messages"][0]["receive_count"] == 5
    assert (
        payload["messages"][0]["parser_error"]
        == "invalid aws_sns_cloudwatch_alarm message: missing TopicArn"
    )
    assert payload["messages"][0]["body_summary"] == {
        "type": "Notification",
        "keys": ["Message", "MessageId", "Type"],
        "message_keys": ["AlarmName", "SecretAccessKey"],
    }
    assert payload["messages"][0]["message_attribute_keys"] == ["AlarmType", "Authorization"]
    assert fake_sqs.receive_calls == [
        {
            "QueueUrl": "https://sqs.example/dlq",
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": 0,
            "VisibilityTimeout": 0,
            "AttributeNames": ["ApproximateReceiveCount", "SentTimestamp"],
            "MessageAttributeNames": ["All"],
        }
    ]
    assert "receipt-secret" not in output
    assert "secret-token" not in output
    assert "super-secret-value" not in output
    assert "sns-dlq-secret-id" not in output


def test_dlq_inspect_rejects_zero_max_messages(tmp_path: Path) -> None:
    config_file = _write_ops_config(tmp_path)

    with pytest.raises(ValueError, match="between 1 and 10"):
        main(
            [
                "--config",
                str(config_file),
                "dlq-inspect",
                "--source",
                "ticketdovale-prod-alerts",
                "--max-messages",
                "0",
            ],
            env=_env(),
        )


def test_dlq_inspect_does_not_echo_untrusted_type_values(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_ops_config(tmp_path)
    fake_sqs = CustomReceiveSqsClient(
        region_name="sa-east-1",
        response={
            "Messages": [
                {
                    "MessageId": "poisoned-type",
                    "Body": json.dumps(
                        {"Type": "secret-type-token", "Message": json.dumps({"AlarmName": "x"})}
                    ),
                }
            ]
        },
    )
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient", lambda *, region_name: fake_sqs
    )

    code = main(
        ["--config", str(config_file), "dlq-inspect", "--source", "ticketdovale-prod-alerts"],
        env=_env(),
    )

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["messages"][0]["body_summary"]["type"] == "json"
    assert "secret-type-token" not in output


def test_dlq_inspect_requires_configured_dlq_url(tmp_path: Path) -> None:
    config_file = _write_ops_config(tmp_path)

    with pytest.raises(ValueError, match="missing DLQ URL"):
        main(
            ["--config", str(config_file), "dlq-inspect", "--source", "ticketdovale-prod-alerts"],
            env={
                key: value
                for key, value in _env().items()
                if key != "TICKETDOVALE_AGENT_ALERT_DLQ_URL"
            },
        )
