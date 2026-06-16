from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_alert_monitor.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSqsClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.receive_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.receive_calls.append(kwargs)
        return self.response

    def delete_message(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls.append(kwargs)
        return {}


class RaisingSqsClient:
    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDenied for https://sqs.example/secret-account-queue")

    def delete_message(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("DeleteMessage should not run")


def _write_sqs_config(tmp_path: Path, *, envelope: str = "aws_sns_cloudwatch_alarm") -> Path:
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
        region: sa-east-1
        envelope: {envelope}
        wait_time_seconds: 7
        max_messages: 3
        visibility_timeout_seconds: 45
    sinks:
      - name: ticketdovale-telegram-status
        type: telegram
        chat_id: "-100111"
    hermes:
      coordinator_profile: alert-coordinator
    kanban:
      incident_assignee: debugger
""".strip(),
        encoding="utf-8",
    )
    return config_file


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_sqs_peek_receives_and_prints_normalized_alert_without_deleting(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_sqs_config(tmp_path)
    fake_client = FakeSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: fake_client,
    )

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-peek",
            "--source",
            "ticketdovale-prod-alerts",
            "--max-messages",
            "1",
        ],
        env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
    )

    assert code == 0
    assert fake_client.receive_calls == [
        {
            "QueueUrl": "https://sqs.example/queue",
            "MaxNumberOfMessages": 1,
            "WaitTimeSeconds": 7,
            "VisibilityTimeout": 45,
            "AttributeNames": ["ApproximateReceiveCount", "SentTimestamp"],
            "MessageAttributeNames": ["All"],
        }
    ]
    assert fake_client.delete_calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "ticketdovale-prod-alerts"
    assert payload["dry_run"] is True
    assert payload["messages_received"] == 1
    assert payload["messages"][0]["ok"] is True
    assert payload["messages"][0]["event_id"].endswith(":sns-message-alarm-1")
    assert payload["messages"][0]["transition_key"].endswith(":ALARM:2026-06-16T12:34:56Z")
    assert payload["messages"][0]["incident_fingerprint"].startswith("cloudwatch-alarm:")
    assert payload["messages"][0]["normalized_alert"]["alarm_name"] == (
        "payment-processor-prod-lambda-errors-alarm"
    )
    assert "receipt" not in json.dumps(payload).lower()


def test_sqs_peek_resolves_config_generated_source_name(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: {tmp_path / "state"}
projects:
  - slug: ticketdovale
    sources:
      - type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    hermes:
      coordinator_profile: alert-coordinator
    kanban:
      incident_assignee: debugger
""".strip(),
        encoding="utf-8",
    )
    fake_client = FakeSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: fake_client,
    )

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-peek",
            "--source",
            "ticketdovale-aws_sqs-1",
        ],
        env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "ticketdovale-aws_sqs-1"
    assert payload["messages"][0]["ok"] is True


def test_sqs_peek_expands_env_source_name_before_project_lookup(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: {tmp_path / "state"}
projects:
  - slug: ticketdovale
    sources:
      - name: ${{TICKETDOVALE_SOURCE_NAME}}
        type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    hermes:
      coordinator_profile: alert-coordinator
    kanban:
      incident_assignee: debugger
""".strip(),
        encoding="utf-8",
    )
    fake_client = FakeSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: fake_client,
    )

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-peek",
            "--source",
            "ticketdovale-env-alerts",
        ],
        env={
            "TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue",
            "TICKETDOVALE_SOURCE_NAME": "ticketdovale-env-alerts",
        },
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "ticketdovale-env-alerts"
    assert payload["messages"][0]["ok"] is True


def test_sqs_peek_does_not_require_unrelated_sink_env(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: {tmp_path / "state"}
projects:
  - slug: ticketdovale
    sources:
      - name: ticketdovale-prod-alerts
        type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    sinks:
      - name: ticketdovale-telegram-status
        type: telegram
        bot_token_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN
        chat_id_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID
    hermes:
      coordinator_profile: alert-coordinator
    kanban:
      incident_assignee: debugger
      default_priority: ${{KANBAN_DEFAULT_PRIORITY}}
""".strip(),
        encoding="utf-8",
    )
    fake_client = FakeSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: fake_client,
    )

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-peek",
            "--source",
            "ticketdovale-prod-alerts",
        ],
        env={
            "TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue",
            "ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN": "",
            "ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID": "",
            "KANBAN_DEFAULT_PRIORITY": "",
        },
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "ticketdovale-prod-alerts"
    assert payload["messages"][0]["ok"] is True


def test_sqs_peek_requires_selected_source_receive_setting_env(
    tmp_path: Path, monkeypatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: {tmp_path / "state"}
projects:
  - slug: ticketdovale
    sources:
      - name: ticketdovale-prod-alerts
        type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
        max_messages: ${{SQS_MAX_MESSAGES}}
    sinks:
      - name: ticketdovale-telegram-status
        type: telegram
        chat_id_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID
    hermes:
      coordinator_profile: alert-coordinator
    kanban:
      incident_assignee: debugger
""".strip(),
        encoding="utf-8",
    )
    fake_client = FakeSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: fake_client,
    )

    with pytest.raises(ValueError, match="SQS_MAX_MESSAGES"):
        main(
            [
                "--config",
                str(config_file),
                "sqs-peek",
                "--source",
                "ticketdovale-prod-alerts",
            ],
            env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
        )


def test_sqs_peek_max_messages_override_skips_configured_batch_env(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: {tmp_path / "state"}
projects:
  - slug: ticketdovale
    sources:
      - name: ticketdovale-prod-alerts
        type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
        max_messages: ${{SQS_MAX_MESSAGES}}
    hermes:
      coordinator_profile: alert-coordinator
    kanban:
      incident_assignee: debugger
""".strip(),
        encoding="utf-8",
    )
    fake_client = FakeSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: fake_client,
    )

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-peek",
            "--source",
            "ticketdovale-prod-alerts",
            "--max-messages",
            "2",
        ],
        env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
    )

    assert code == 0
    assert fake_client.receive_calls[0]["MaxNumberOfMessages"] == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["messages"][0]["ok"] is True


def test_sqs_ingest_dry_run_uses_configured_batch_size_and_does_not_delete_or_mutate(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_sqs_config(tmp_path, envelope="aws_eventbridge_cloudwatch_alarm")
    fake_client = FakeSqsClient(_fixture("aws_sqs_receive_message_eventbridge_envelope.json"))
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: fake_client,
    )

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-ingest",
            "--source",
            "ticketdovale-prod-alerts",
            "--dry-run",
        ],
        env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
    )

    assert code == 0
    assert fake_client.receive_calls[0]["MaxNumberOfMessages"] == 3
    assert fake_client.delete_calls == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["mutates_incidents"] is False
    assert payload["deletes_messages"] is False
    assert payload["messages"][0]["event_id"] == (
        "eventbridge:123456789012:sa-east-1:eventbridge-alarm-1"
    )


def test_sqs_receive_errors_are_sanitized(tmp_path: Path, monkeypatch) -> None:
    config_file = _write_sqs_config(tmp_path)
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: RaisingSqsClient(),
    )

    with pytest.raises(RuntimeError) as excinfo:
        main(
            [
                "--config",
                str(config_file),
                "sqs-peek",
                "--source",
                "ticketdovale-prod-alerts",
            ],
            env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
        )

    error = str(excinfo.value)
    assert error == "SQS receive failed for source ticketdovale-prod-alerts: RuntimeError"
    assert "https://sqs.example" not in error
    assert "secret-account" not in error


def test_sqs_dry_run_parse_errors_are_visible_and_sanitized(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    config_file = _write_sqs_config(tmp_path)
    fake_client = FakeSqsClient(
        {
            "Messages": [
                {
                    "MessageId": "sqs-bad-1",
                    "ReceiptHandle": "secret-receipt-handle",
                    "Body": json.dumps({"Type": "Notification", "MessageId": "sns-secret-id"}),
                }
            ]
        }
    )
    monkeypatch.setattr(
        "agent_alert_monitor.sqs_ingest.Boto3SqsClient",
        lambda *, region_name: fake_client,
    )

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-ingest",
            "--source",
            "ticketdovale-prod-alerts",
            "--dry-run",
        ],
        env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
    )

    assert code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["messages"][0] == {
        "ok": False,
        "message_id": "sqs-bad-1",
        "error": "invalid aws_sns_cloudwatch_alarm message: missing TopicArn",
    }
    assert "secret-receipt-handle" not in output
    assert "sns-secret-id" not in output
    assert fake_client.delete_calls == []
