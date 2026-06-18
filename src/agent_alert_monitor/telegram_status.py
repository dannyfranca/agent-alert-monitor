from __future__ import annotations

import requests

from .config import AgentConfig


def send_telegram_message(config: AgentConfig, text: str) -> None:
    _require_bot_token(config)
    response = _telegram_post(
        config, "sendMessage", json={"chat_id": config.telegram.alert_chat_id, "text": text}
    )
    _raise_for_status(response)


def _telegram_url(config: AgentConfig, method: str) -> str:
    return f"https://api.telegram.org/bot{config.telegram.bot_token}/{method}"


def _telegram_post(config: AgentConfig, method: str, json: dict[str, str]) -> requests.Response:
    try:
        return requests.post(_telegram_url(config, method), json=json, timeout=30)
    except requests.RequestException as exc:
        raise _sanitized_request_error(method, exc) from None


def _sanitized_request_error(method: str, exc: requests.RequestException) -> RuntimeError:
    return RuntimeError(f"Telegram API {method} request failed: {type(exc).__name__}")


def _require_bot_token(config: AgentConfig) -> None:
    if not config.telegram.bot_token:
        raise ValueError(
            f"missing Telegram bot token; set {config.telegram.bot_token_env} "
            "before sending Telegram messages"
        )


def _raise_for_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError:
        status = getattr(response, "status_code", "unknown")
        raise RuntimeError(f"Telegram API request failed with HTTP status {status}") from None
