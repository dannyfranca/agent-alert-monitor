from __future__ import annotations

import json
import subprocess
import sys
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_alert_monitor.cli import main
from agent_alert_monitor.kanban import KanbanCardRequest
from agent_alert_monitor.ledger import AlertLedger
from agent_alert_monitor.sqs_ingest import (
    PreflightResult,
    find_sqs_source,
    listen_for_sqs_messages,
    run_local_preflight,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeSqsClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.receive_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.receive_calls.append(kwargs)
        return self.response

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        return {}

    def delete_message(self, **kwargs: Any) -> dict[str, Any]:
        self.delete_calls.append(kwargs)
        return {}


class RaisingSqsClient:
    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("AccessDenied for https://sqs.example/secret-account-queue")

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("GetQueueAttributes should not run")

    def delete_message(self, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("DeleteMessage should not run")


class RecordingSqsClient(FakeSqsClient):
    def __init__(
        self,
        response: dict[str, Any],
        *,
        on_delete: Any | None = None,
    ) -> None:
        super().__init__(response)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.on_delete = on_delete

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("receive", kwargs))
        return super().receive_message(**kwargs)

    def delete_message(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete", kwargs))
        if self.on_delete is not None:
            self.on_delete(kwargs)
        return super().delete_message(**kwargs)


class QueueArnMismatchSqsClient(RecordingSqsClient):
    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        return {"Attributes": {"QueueArn": "arn:aws:sqs:sa-east-1:123456789012:wrong-queue"}}


class FakeKanbanClient:
    def __init__(self, *, fail_create: bool = False, fail_comment: bool = False) -> None:
        self.fail_create = fail_create
        self.fail_comment = fail_comment
        self.created: list[KanbanCardRequest] = []
        self.comments: list[tuple[str, str]] = []

    def create_incident(self, request: KanbanCardRequest) -> str:
        if self.fail_create:
            raise RuntimeError("kanban unavailable")
        self.created.append(request)
        return f"t_fake_{len(self.created)}"

    def comment(self, task_id: str, body: str) -> None:
        if self.fail_comment:
            raise RuntimeError("kanban comment unavailable")
        self.comments.append((task_id, body))


class FakeStsClient:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "123456789012"}


def _patch_successful_preflight_processes(monkeypatch, cfg: Any) -> None:
    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command == ["/bin/hermes", "profile", "list"]:
            stdout = "default\nalert-coordinator\n"
        elif command == ["/bin/hermes", "-p", "alert-coordinator", "kanban", "boards", "list"]:
            stdout = f"default\n{cfg.hermes.kanban_board}\n"
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("agent_alert_monitor.sqs_ingest.shutil.which", lambda name: "/bin/hermes")
    monkeypatch.setattr("agent_alert_monitor.sqs_ingest.subprocess.run", fake_run)
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_, **__: FakeStsClient()),
    )


def test_local_preflight_uses_hermes_cli_profile_and_board_checks(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = _live_cfg(tmp_path)
    source = find_sqs_source(cfg, "ticketdovale-prod-alerts")
    ledger = AlertLedger(cfg.runtime.ledger_path)
    client = RecordingSqsClient({"Messages": []})
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command == ["/bin/hermes", "profile", "list"]:
            stdout = "default\nalert-coordinator\n"
        elif command == ["/bin/hermes", "-p", "alert-coordinator", "kanban", "boards", "list"]:
            stdout = f"default\n{cfg.hermes.kanban_board}\n"
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setenv("HOME", str(tmp_path / "home-without-profile-dir"))
    monkeypatch.setattr("agent_alert_monitor.sqs_ingest.shutil.which", lambda name: "/bin/hermes")
    monkeypatch.setattr("agent_alert_monitor.sqs_ingest.subprocess.run", fake_run)
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=lambda *_, **__: FakeStsClient()),
    )

    result = run_local_preflight(cfg, source, ledger, client)

    assert result.ok is True
    assert result.checks["hermes_profile"] == "ok"
    assert result.checks["kanban_board"] == "ok"
    assert commands == [
        ["/bin/hermes", "profile", "list"],
        ["/bin/hermes", "-p", "alert-coordinator", "kanban", "boards", "list"],
    ]


def test_local_preflight_fails_on_queue_arn_mismatch(tmp_path: Path, monkeypatch) -> None:
    cfg = _live_cfg(tmp_path)
    source = replace(
        find_sqs_source(cfg, "ticketdovale-prod-alerts"),
        queue_arn="arn:aws:sqs:sa-east-1:123456789012:expected-queue",
    )
    ledger = AlertLedger(cfg.runtime.ledger_path)
    client = QueueArnMismatchSqsClient({"Messages": []})
    _patch_successful_preflight_processes(monkeypatch, cfg)

    result = run_local_preflight(cfg, source, ledger, client)

    assert result.ok is False
    assert result.checks["sqs_queue_access"] == "failed: arn_mismatch"
    assert client.receive_calls == []


def _live_cfg(tmp_path: Path, *, envelope: str = "aws_sns_cloudwatch_alarm"):
    config_file = _write_sqs_config(tmp_path, envelope=envelope)
    from agent_alert_monitor.config import load_config

    return load_config(
        config_file,
        project_slug="ticketdovale",
        env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
    )


def _preflight_ok() -> PreflightResult:
    return PreflightResult(ok=True, checks={"sqlite": "ok", "aws_identity": "ok"})


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
      kanban_board: default
    kanban:
      incident_assignee: debugger
""".strip(),
        encoding="utf-8",
    )
    return config_file


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _sqs_response_from_sns_fixture(
    fixture_name: str,
    *,
    sqs_message_id: str,
    receipt_handle: str,
    receive_count: str = "1",
) -> dict[str, Any]:
    return {
        "Messages": [
            {
                "MessageId": sqs_message_id,
                "ReceiptHandle": receipt_handle,
                "Attributes": {"ApproximateReceiveCount": receive_count},
                "MessageAttributes": {},
                "Body": json.dumps(_fixture(fixture_name)),
            }
        ]
    }


def _sqs_response_with_sns_overrides(
    fixture_name: str,
    *,
    sqs_message_id: str,
    sns_message_id: str,
    receipt_handle: str,
    cloudwatch_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    envelope = _fixture(fixture_name)
    envelope["MessageId"] = sns_message_id
    cloudwatch_message = json.loads(envelope["Message"])
    cloudwatch_message.update(cloudwatch_overrides or {})
    envelope["Message"] = json.dumps(cloudwatch_message)
    return {
        "Messages": [
            {
                "MessageId": sqs_message_id,
                "ReceiptHandle": receipt_handle,
                "Attributes": {"ApproximateReceiveCount": "1"},
                "MessageAttributes": {},
                "Body": json.dumps(envelope),
            }
        ]
    }


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


def test_sqs_listen_preflight_failure_avoids_receive_message(tmp_path: Path) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    client = RecordingSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))

    result = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        client=client,
        preflight=lambda: PreflightResult(ok=False, checks={"hermes_binary": "failed"}),
        once=True,
    )

    assert result["preflight_ok"] is False
    assert result["messages_received"] == 0
    assert client.receive_calls == []
    assert client.delete_calls == []


def test_sqs_listen_live_success_creates_incident_and_deletes_after_side_effects(
    tmp_path: Path,
) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)

    def assert_side_effects_committed_before_delete(_kwargs: dict[str, Any]) -> None:
        with ledger.connect() as conn:
            incident = conn.execute("SELECT kanban_task_id FROM alert_incidents").fetchone()
            effect = conn.execute(
                """
                SELECT status FROM alert_side_effects
                WHERE effect_name='required_side_effects'
                """
            ).fetchone()
        assert incident["kanban_task_id"] == "t_fake_1"
        assert effect["status"] == "succeeded"

    client = RecordingSqsClient(
        _fixture("aws_sqs_receive_message_sns_envelope.json"),
        on_delete=assert_side_effects_committed_before_delete,
    )
    kanban = FakeKanbanClient()
    telegram_messages: list[str] = []

    result = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=kanban,
        client=client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, text: telegram_messages.append(text),
        once=True,
    )

    assert result["preflight_ok"] is True
    assert result["messages_received"] == 1
    assert result["messages"][0]["action"] == "opened"
    assert result["messages"][0]["deleted"] is True
    assert len(kanban.created) == 1
    assert "# CloudWatch Alert Incident" in kanban.created[0].body
    assert telegram_messages and "t_fake_1" in telegram_messages[0]
    assert [name for name, _kwargs in client.calls] == ["receive", "delete"]
    assert client.delete_calls == [
        {"QueueUrl": "https://sqs.example/queue", "ReceiptHandle": "sanitized-receipt-handle"}
    ]
    with ledger.connect() as conn:
        event = conn.execute("SELECT parse_status FROM alert_events").fetchone()
        incident = conn.execute("SELECT kanban_task_id FROM alert_incidents").fetchone()
        effect = conn.execute(
            """
            SELECT status FROM alert_side_effects
            WHERE effect_name='required_side_effects'
            """
        ).fetchone()
    assert event["parse_status"] == "parsed"
    assert incident["kanban_task_id"] == "t_fake_1"
    assert effect["status"] == "succeeded"


def test_sqs_listen_uses_default_telegram_status_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    client = RecordingSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    sent: list[tuple[str, str]] = []

    def fake_send(config, text: str) -> None:
        sent.append((config.project_slug, text))

    monkeypatch.setattr("agent_alert_monitor.sqs_ingest.send_telegram_message", fake_send)

    result = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        client=client,
        preflight=_preflight_ok,
        once=True,
    )

    assert result["messages"][0]["action"] == "opened"
    assert sent
    assert sent[0][0] == "ticketdovale"
    assert "t_fake_1" in sent[0][1]


def test_sqs_listen_redelivery_after_success_does_not_duplicate_card(tmp_path: Path) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    kanban = FakeKanbanClient()
    first_client = RecordingSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    second_client = RecordingSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))

    first = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=kanban,
        client=first_client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, _text: None,
        once=True,
    )
    second = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=kanban,
        client=second_client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, _text: None,
        once=True,
    )

    assert first["messages"][0]["action"] == "opened"
    assert second["messages"][0]["action"] == "duplicate_event"
    assert len(kanban.created) == 1
    assert second_client.delete_calls == [
        {"QueueUrl": "https://sqs.example/queue", "ReceiptHandle": "sanitized-receipt-handle"}
    ]


def test_duplicate_transition_is_ledger_only_and_deletes_without_side_effects(
    tmp_path: Path,
) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    first_client = RecordingSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    duplicate_client = RecordingSqsClient(
        _sqs_response_with_sns_overrides(
            "aws_sns_cloudwatch_alarm_alarm.json",
            sqs_message_id="sqs-alarm-duplicate-transition",
            sns_message_id="sns-message-alarm-duplicate-transition",
            receipt_handle="duplicate-transition-receipt",
        )
    )
    kanban = FakeKanbanClient(fail_comment=True)
    sent: list[str] = []

    opened = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=kanban,
        client=first_client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, text: sent.append(text),
        once=True,
    )
    sent.clear()
    duplicate = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=kanban,
        client=duplicate_client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, _text: (_ for _ in ()).throw(RuntimeError("status down")),
        once=True,
    )

    assert opened["messages"][0]["action"] == "opened"
    assert duplicate["messages"][0]["action"] == "duplicate_transition"
    assert duplicate["messages"][0]["deleted"] is True
    assert duplicate_client.delete_calls == [
        {"QueueUrl": "https://sqs.example/queue", "ReceiptHandle": "duplicate-transition-receipt"}
    ]
    assert len(kanban.created) == 1
    assert kanban.comments == []
    assert sent == []
    with ledger.connect() as conn:
        duplicate_event = conn.execute(
            "SELECT parse_status FROM alert_events WHERE event_id LIKE ?",
            ("%sns-message-alarm-duplicate-transition",),
        ).fetchone()
    assert duplicate_event is not None
    assert duplicate_event["parse_status"] == "parsed"


def test_sqs_listen_malformed_cloud_timestamp_is_parse_failed_not_crash(
    tmp_path: Path,
) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    client = RecordingSqsClient(
        _sqs_response_with_sns_overrides(
            "aws_sns_cloudwatch_alarm_alarm.json",
            sqs_message_id="sqs-bad-timestamp",
            sns_message_id="sns-bad-timestamp",
            receipt_handle="bad-timestamp-receipt",
            cloudwatch_overrides={"StateChangeTime": "not-a-timestamp"},
        )
    )
    result = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        client=client,
        preflight=_preflight_ok,
        once=True,
    )
    assert result["messages"][0]["action"] == "parse_failed"
    assert result["messages"][0]["deleted"] is False
    assert client.delete_calls == []
    with ledger.connect() as conn:
        event = conn.execute(
            "SELECT parse_status, parse_error FROM alert_events WHERE event_id LIKE ?",
            ("parse-failed:ticketdovale-prod-alerts:sqs-bad-timestamp:%",),
        ).fetchone()
    assert event is not None
    assert event["parse_status"] == "failed"
    assert "StateChangeTime" in event["parse_error"]


def test_sqs_listen_kanban_failure_persists_event_but_does_not_delete(tmp_path: Path) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    client = RecordingSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))

    result = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(fail_create=True),
        client=client,
        preflight=_preflight_ok,
        once=True,
    )

    assert result["messages"][0]["action"] == "opened"
    assert result["messages"][0]["deleted"] is False
    assert result["messages"][0]["error"] == "RuntimeError"
    assert client.delete_calls == []
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM alert_incidents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM alert_side_effects").fetchone()[0] == 0


def test_sqs_listen_parse_failure_is_persisted_and_left_for_retry(tmp_path: Path) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    client = RecordingSqsClient(
        {
            "Messages": [
                {
                    "MessageId": "sqs-bad-1",
                    "ReceiptHandle": "bad-receipt",
                    "Body": json.dumps({"Type": "Notification", "MessageId": "sns-secret-id"}),
                }
            ]
        }
    )

    result = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        client=client,
        preflight=_preflight_ok,
        once=True,
    )

    assert result["messages"][0]["action"] == "parse_failed"
    assert result["messages"][0]["deleted"] is False
    assert client.delete_calls == []
    with ledger.connect() as conn:
        event = conn.execute(
            "SELECT event_id, parse_status, parse_error, raw_sqs_message_json FROM alert_events"
        ).fetchone()
    assert event["event_id"].startswith("parse-failed:ticketdovale-prod-alerts:sqs-bad-1:")
    assert event["parse_status"] == "failed"
    assert event["parse_error"] == "invalid aws_sns_cloudwatch_alarm message: missing TopicArn"
    assert "bad-receipt" not in event["raw_sqs_message_json"]


def test_sqs_listen_telegram_status_failure_does_not_block_delete(tmp_path: Path) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    client = RecordingSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))

    result = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        client=client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, _text: (_ for _ in ()).throw(RuntimeError("telegram down")),
        once=True,
    )

    assert result["messages"][0]["action"] == "opened"
    assert result["messages"][0]["deleted"] is True
    assert client.delete_calls


def test_sqs_listen_retries_required_kanban_comment_before_delete(tmp_path: Path) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    kanban = FakeKanbanClient()
    alarm_client = RecordingSqsClient(_fixture("aws_sqs_receive_message_sns_envelope.json"))
    failing_ok_client = RecordingSqsClient(
        _sqs_response_from_sns_fixture(
            "aws_sns_cloudwatch_alarm_ok.json",
            sqs_message_id="sqs-ok-1",
            receipt_handle="ok-receipt-1",
        )
    )
    retry_ok_client = RecordingSqsClient(
        _sqs_response_from_sns_fixture(
            "aws_sns_cloudwatch_alarm_ok.json",
            sqs_message_id="sqs-ok-1",
            receipt_handle="ok-receipt-2",
            receive_count="2",
        )
    )

    opened = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=kanban,
        client=alarm_client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, _text: None,
        once=True,
    )
    failed_ok = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(fail_comment=True),
        client=failing_ok_client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, _text: None,
        once=True,
    )
    retried_ok = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=kanban,
        client=retry_ok_client,
        preflight=_preflight_ok,
        status_sender=lambda _cfg, _text: None,
        once=True,
    )

    assert opened["messages"][0]["action"] == "opened"
    assert failed_ok["messages"][0]["action"] == "resolved"
    assert failed_ok["messages"][0]["deleted"] is False
    assert failing_ok_client.delete_calls == []
    assert retried_ok["messages"][0]["action"] == "duplicate_event"
    assert retried_ok["messages"][0]["deleted"] is True
    assert retry_ok_client.delete_calls == [
        {"QueueUrl": "https://sqs.example/queue", "ReceiptHandle": "ok-receipt-2"}
    ]
    assert kanban.comments
    assert "Recovered with retried duplicate event" in kanban.comments[-1][1]


def test_sqs_listen_parse_failure_id_is_stable_across_receive_count_changes(
    tmp_path: Path,
) -> None:
    cfg = _live_cfg(tmp_path)
    ledger = AlertLedger(cfg.runtime.ledger_path)
    first_client = RecordingSqsClient(
        {
            "Messages": [
                {
                    "MessageId": "sqs-bad-1",
                    "ReceiptHandle": "bad-receipt-1",
                    "Attributes": {"ApproximateReceiveCount": "1"},
                    "Body": json.dumps({"Type": "Notification", "MessageId": "sns-secret-id"}),
                }
            ]
        }
    )
    second_client = RecordingSqsClient(
        {
            "Messages": [
                {
                    "MessageId": "sqs-bad-1",
                    "ReceiptHandle": "bad-receipt-2",
                    "Attributes": {"ApproximateReceiveCount": "2"},
                    "Body": json.dumps({"Type": "Notification", "MessageId": "sns-secret-id"}),
                }
            ]
        }
    )

    first = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        client=first_client,
        preflight=_preflight_ok,
        once=True,
    )
    second = listen_for_sqs_messages(
        cfg,
        source_name="ticketdovale-prod-alerts",
        ledger=ledger,
        kanban_client=FakeKanbanClient(),
        client=second_client,
        preflight=_preflight_ok,
        once=True,
    )

    assert first["messages"][0]["event_id"] == second["messages"][0]["event_id"]
    with ledger.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_events").fetchone()[0] == 1


def test_cli_sqs_listen_once_dispatches_live_listener(tmp_path: Path, capsys, monkeypatch) -> None:
    config_file = _write_sqs_config(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_listen(cfg, **kwargs: Any) -> dict[str, Any]:
        calls.append({"project": cfg.project_slug, **kwargs})
        return {"preflight_ok": True, "messages_received": 0, "messages": []}

    monkeypatch.setattr("agent_alert_monitor.sqs_ingest.listen_for_sqs_messages", fake_listen)

    code = main(
        [
            "--config",
            str(config_file),
            "sqs-listen",
            "--source",
            "ticketdovale-prod-alerts",
            "--once",
        ],
        env={"TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.example/queue"},
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["messages_received"] == 0
    assert calls[0]["project"] == "ticketdovale"
    assert calls[0]["source_name"] == "ticketdovale-prod-alerts"
    assert calls[0]["once"] is True
