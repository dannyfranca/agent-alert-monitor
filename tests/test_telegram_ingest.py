from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

import pytest
import requests

from agent_alert_monitor.config import (
    AgentConfig,
    HermesConfig,
    KanbanConfig,
    RuntimeConfig,
    TelegramConfig,
    WatchdogConfig,
)
from agent_alert_monitor.coordinator import AlertCoordinator
from agent_alert_monitor.kanban import KanbanCardRequest
from agent_alert_monitor.ledger import AlertLedger
from agent_alert_monitor.telegram_ingest import poll_once, poll_once_many, send_telegram_message


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, Any],
        status_code: int = 200,
        http_error_url: str | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.http_error_url = http_error_url

    def raise_for_status(self) -> None:
        if self.http_error_url is not None:
            raise requests.HTTPError(
                f"{self.status_code} Server Error for url: {self.http_error_url}"
            )

    def json(self) -> dict[str, Any]:
        return self.payload


class RecordingKanbanClient:
    def create_incident(self, request: KanbanCardRequest) -> str:
        return "t_00000001"

    def comment(self, task_id: str, body: str) -> None:
        raise AssertionError("comments are not used by this test")


def make_config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        telegram=TelegramConfig(
            bot_token="test-token",
            alert_chat_id="-100123",
            offset_path=tmp_path / "telegram-offset.json",
        ),
        hermes=HermesConfig(coordinator_profile="alert-coordinator"),
        kanban=KanbanConfig(incident_assignee="debugger"),
        runtime=RuntimeConfig(state_dir=tmp_path, ledger_path=tmp_path / "ledger.sqlite"),
        watchdog=WatchdogConfig(),
    )


def telegram_payload() -> dict[str, Any]:
    return {
        "ok": True,
        "result": [
            {
                "update_id": 42,
                "channel_post": {
                    "message_id": 7,
                    "chat": {"id": -100123},
                    "text": "ALARM: Service5xx service=api",
                },
            }
        ],
    }


def config_with_token(config: AgentConfig, token: str) -> AgentConfig:
    return AgentConfig(
        telegram=TelegramConfig(
            bot_token=token,
            alert_chat_id=config.telegram.alert_chat_id,
            offset_path=config.telegram.offset_path,
        ),
        hermes=config.hermes,
        kanban=config.kanban,
        runtime=config.runtime,
        watchdog=config.watchdog,
    )


def formatted_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc))


def test_dry_run_poll_does_not_advance_telegram_offset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "agent_alert_monitor.telegram_ingest.requests.get",
        lambda *args, **kwargs: FakeResponse(telegram_payload()),
    )
    config = make_config(tmp_path)
    coordinator = AlertCoordinator(config, ledger=AlertLedger(config.runtime.ledger_path))

    results = poll_once(config, coordinator, dry_run=True)

    assert len(results) == 1
    assert results[0].action == "would_create_incident"
    assert config.telegram.offset_path is not None
    assert not config.telegram.offset_path.exists()


def test_live_poll_sends_ack_before_advancing_offset(tmp_path: Path, monkeypatch) -> None:
    sent_messages: list[str] = []
    monkeypatch.setattr(
        "agent_alert_monitor.telegram_ingest.requests.get",
        lambda *args, **kwargs: FakeResponse(telegram_payload()),
    )

    def fake_post(*args, **kwargs):
        sent_messages.append(kwargs["json"]["text"])
        return FakeResponse({"ok": True})

    monkeypatch.setattr("agent_alert_monitor.telegram_ingest.requests.post", fake_post)
    config = make_config(tmp_path)
    coordinator = AlertCoordinator(
        config,
        ledger=AlertLedger(config.runtime.ledger_path),
        kanban_client=RecordingKanbanClient(),
    )

    results = poll_once(config, coordinator, dry_run=False)

    assert len(results) == 1
    assert sent_messages == [results[0].channel_message]
    assert config.telegram.offset_path is not None
    assert config.telegram.offset_path.exists()
    incident = coordinator.ledger.get_incident(results[0].incident_task_id or "")
    assert incident is not None
    assert incident.last_channel_status == "acked"


def test_shared_bot_poll_skips_updates_older_than_project_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha_offset = tmp_path / "alpha-offset.json"
    beta_offset = tmp_path / "beta-offset.json"
    alpha_offset.write_text('{"offset": 50}\n', encoding="utf-8")
    beta_offset.write_text('{"offset": 40}\n', encoding="utf-8")
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 45,
                "channel_post": {
                    "message_id": 1,
                    "chat": {"id": -100111},
                    "text": "ALARM: Service5xx service=api",
                },
            },
            {
                "update_id": 46,
                "channel_post": {
                    "message_id": 2,
                    "chat": {"id": -100222},
                    "text": "ALARM: QueueDepth service=worker",
                },
            },
        ],
    }
    monkeypatch.setattr(
        "agent_alert_monitor.telegram_ingest.requests.get",
        lambda *args, **kwargs: FakeResponse(payload),
    )
    base = make_config(tmp_path)
    alpha = AgentConfig(
        telegram=TelegramConfig(
            bot_token="shared-token", alert_chat_id="-100111", offset_path=alpha_offset
        ),
        hermes=base.hermes,
        kanban=base.kanban,
        runtime=base.runtime,
        watchdog=base.watchdog,
        project_slug="alpha-api",
        project_display_name="Alpha API",
    )
    beta = AgentConfig(
        telegram=TelegramConfig(
            bot_token="shared-token", alert_chat_id="-100222", offset_path=beta_offset
        ),
        hermes=base.hermes,
        kanban=base.kanban,
        runtime=base.runtime,
        watchdog=base.watchdog,
        project_slug="beta-worker",
        project_display_name="Beta Worker",
    )
    coordinators = {
        "alpha-api": AlertCoordinator(alpha, ledger=AlertLedger(base.runtime.ledger_path)),
        "beta-worker": AlertCoordinator(beta, ledger=AlertLedger(base.runtime.ledger_path)),
    }

    results = poll_once_many([alpha, beta], coordinators, dry_run=True)

    assert [project for project, _ in results] == ["beta-worker"]


def test_poll_request_failure_does_not_expose_bot_token(tmp_path: Path, monkeypatch) -> None:
    secret_token = "123456:super-secret-token"
    leaked_url = f"https://api.telegram.org/bot{secret_token}/getUpdates"

    def fail_get(*args, **kwargs):
        raise requests.Timeout(f"timed out while requesting {leaked_url}")

    monkeypatch.setattr("agent_alert_monitor.telegram_ingest.requests.get", fail_get)
    config = config_with_token(make_config(tmp_path), secret_token)
    coordinator = AlertCoordinator(config, ledger=AlertLedger(config.runtime.ledger_path))

    with pytest.raises(RuntimeError) as excinfo:
        poll_once(config, coordinator, dry_run=True)

    message = str(excinfo.value)
    trace = formatted_exception(excinfo.value)
    assert secret_token not in message
    assert leaked_url not in message
    assert secret_token not in trace
    assert leaked_url not in trace
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "getUpdates" in message


def test_send_request_failure_does_not_expose_bot_token(tmp_path: Path, monkeypatch) -> None:
    secret_token = "123456:super-secret-token"
    leaked_url = f"https://api.telegram.org/bot{secret_token}/sendMessage"

    def fail_post(*args, **kwargs):
        raise requests.ConnectionError(f"dns failure for {leaked_url}")

    monkeypatch.setattr("agent_alert_monitor.telegram_ingest.requests.post", fail_post)
    config = config_with_token(make_config(tmp_path), secret_token)

    with pytest.raises(RuntimeError) as excinfo:
        send_telegram_message(config, "hello")

    message = str(excinfo.value)
    trace = formatted_exception(excinfo.value)
    assert secret_token not in message
    assert leaked_url not in message
    assert secret_token not in trace
    assert leaked_url not in trace
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "sendMessage" in message


def test_poll_http_status_failure_does_not_expose_bot_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_token = "123456:super-secret-token"
    leaked_url = f"https://api.telegram.org/bot{secret_token}/getUpdates"

    monkeypatch.setattr(
        "agent_alert_monitor.telegram_ingest.requests.get",
        lambda *args, **kwargs: FakeResponse({}, status_code=502, http_error_url=leaked_url),
    )
    config = config_with_token(make_config(tmp_path), secret_token)
    coordinator = AlertCoordinator(config, ledger=AlertLedger(config.runtime.ledger_path))

    with pytest.raises(RuntimeError) as excinfo:
        poll_once(config, coordinator, dry_run=True)

    message = str(excinfo.value)
    trace = formatted_exception(excinfo.value)
    assert secret_token not in message
    assert leaked_url not in message
    assert secret_token not in trace
    assert leaked_url not in trace
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "502" in message


def test_send_http_status_failure_does_not_expose_bot_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_token = "123456:super-secret-token"
    leaked_url = f"https://api.telegram.org/bot{secret_token}/sendMessage"

    monkeypatch.setattr(
        "agent_alert_monitor.telegram_ingest.requests.post",
        lambda *args, **kwargs: FakeResponse({}, status_code=403, http_error_url=leaked_url),
    )
    config = config_with_token(make_config(tmp_path), secret_token)

    with pytest.raises(RuntimeError) as excinfo:
        send_telegram_message(config, "hello")

    message = str(excinfo.value)
    trace = formatted_exception(excinfo.value)
    assert secret_token not in message
    assert leaked_url not in message
    assert secret_token not in trace
    assert leaked_url not in trace
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert "403" in message
