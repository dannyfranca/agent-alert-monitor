from __future__ import annotations

from pathlib import Path

import requests

from agent_alert_monitor.config import (
    AgentConfig,
    HermesConfig,
    KanbanConfig,
    MessageConfig,
    ProjectConfig,
    RuntimeConfig,
    TelegramConfig,
    WatchdogConfig,
)
from agent_alert_monitor.telegram_status import send_telegram_message


def _config() -> AgentConfig:
    telegram = TelegramConfig(
        bot_token_env="ALERT_MONITOR_SAMPLE_API_TELEGRAM_BOT_TOKEN",
        bot_token="secret-token",
        alert_chat_id="-100111",
    )
    hermes = HermesConfig(coordinator_profile="alert-coordinator")
    kanban = KanbanConfig(incident_assignee="debugger")
    project = ProjectConfig(
        slug="sample-api",
        display_name="Sample API",
        telegram=telegram,
        hermes=hermes,
        kanban=kanban,
    )
    return AgentConfig(
        runtime=RuntimeConfig(
            state_dir=Path("./state"), ledger_path=Path("./state/ledger.sqlite")
        ),
        telegram=telegram,
        hermes=hermes,
        kanban=kanban,
        messages=MessageConfig(),
        watchdog=WatchdogConfig(),
        project_slug="sample-api",
        project_display_name="Sample API",
        projects=(project,),
    )


def test_send_telegram_message_posts_to_status_chat(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    def fake_post(url: str, json: dict[str, str], timeout: int):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr("agent_alert_monitor.telegram_status.requests.post", fake_post)

    send_telegram_message(_config(), "hello")

    assert calls == [
        {
            "url": "https://api.telegram.org/botsecret-token/sendMessage",
            "json": {"chat_id": "-100111", "text": "hello"},
            "timeout": 30,
        }
    ]


def test_send_telegram_message_sanitizes_request_errors(monkeypatch) -> None:
    def fake_post(*args, **kwargs):
        raise requests.Timeout("token secret-token leaked")

    monkeypatch.setattr("agent_alert_monitor.telegram_status.requests.post", fake_post)

    try:
        send_telegram_message(_config(), "hello")
    except RuntimeError as exc:
        message = str(exc)
        assert "Timeout" in message
        assert "secret-token" not in message
    else:
        raise AssertionError("expected sanitized Telegram request failure")
