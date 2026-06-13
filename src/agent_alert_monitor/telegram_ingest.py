from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import AgentConfig
from .coordinator import AlertCoordinator, CoordinatorResult
from .message_templates import automation_failure_message


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    chat_id: str
    message_id: str
    text: str


def _extract_channel_posts(payload: dict[str, Any]) -> Iterable[TelegramUpdate]:
    for item in payload.get("result", []):
        post = item.get("channel_post") or item.get("message") or {}
        chat = post.get("chat") or {}
        text = post.get("text") or post.get("caption")
        message_id = post.get("message_id")
        update_id = item.get("update_id")
        chat_id = chat.get("id")
        if update_id is not None and chat_id is not None and message_id is not None and text:
            yield TelegramUpdate(int(update_id), str(chat_id), str(message_id), str(text))


def read_offset(path: Path) -> int | None:
    if not path.exists():
        return None
    return int(json.loads(path.read_text(encoding="utf-8")).get("offset"))


def write_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": offset}, indent=2) + "\n", encoding="utf-8")


def poll_once(
    config: AgentConfig, coordinator: AlertCoordinator, dry_run: bool = True
) -> list[CoordinatorResult]:
    _require_bot_token(config)
    offset_path = config.telegram.offset_path
    offset = read_offset(offset_path) if offset_path else None
    params: dict[str, str | int] = {
        "timeout": 0,
        "allowed_updates": json.dumps(["channel_post", "message"]),
    }
    if offset is not None:
        params["offset"] = offset
    response = _telegram_get(config, "getUpdates", params=params)
    _raise_for_status(response)
    payload = response.json()
    results: list[CoordinatorResult] = []
    max_update_id = offset - 1 if offset is not None else None
    for item in payload.get("result", []):
        if item.get("update_id") is not None:
            update_id = int(item["update_id"])
            max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)
    for update in _extract_channel_posts(payload):
        if update.chat_id != config.telegram.alert_chat_id:
            continue
        try:
            result = coordinator.handle_alert(
                platform="telegram",
                chat_id=update.chat_id,
                message_id=update.message_id,
                raw_text=update.text,
                dry_run=dry_run,
            )
        except Exception as exc:
            if not dry_run:
                send_telegram_message(
                    config,
                    automation_failure_message(
                        f"telegram:{update.chat_id}/{update.message_id}",
                        str(exc),
                        config.messages.prefix,
                    ),
                )
            raise
        if not dry_run and result.incident_task_id and result.channel_message:
            send_telegram_message(config, result.channel_message)
            status = "correlated" if result.action in {"correlated", "duplicate"} else "acked"
            if result.action in {"recovery_matched", "resolved"}:
                status = "final"
            coordinator.record_channel_delivery(result, status)
        results.append(result)
    if max_update_id is not None and offset_path and not dry_run:
        write_offset(offset_path, max_update_id + 1)
    return results


def poll_once_many(
    configs: list[AgentConfig],
    coordinators: dict[str, AlertCoordinator],
    dry_run: bool = True,
) -> list[tuple[str, CoordinatorResult]]:
    """Poll one shared Telegram bot and fan out updates to matching projects."""
    if not configs:
        return []
    config = configs[0]
    _require_bot_token(config)
    offset_by_chat = {
        cfg.telegram.alert_chat_id: read_offset(cfg.telegram.offset_path)
        for cfg in configs
        if cfg.telegram.offset_path
    }
    offsets = [offset for offset in offset_by_chat.values() if offset is not None]
    offset = min(offsets) if offsets else None
    params: dict[str, str | int] = {
        "timeout": 0,
        "allowed_updates": json.dumps(["channel_post", "message"]),
    }
    if offset is not None:
        params["offset"] = offset
    response = _telegram_get(config, "getUpdates", params=params)
    _raise_for_status(response)
    payload = response.json()

    max_update_id = offset - 1 if offset is not None else None
    for item in payload.get("result", []):
        if item.get("update_id") is not None:
            update_id = int(item["update_id"])
            max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)

    chat_ids = [cfg.telegram.alert_chat_id for cfg in configs]
    if len(set(chat_ids)) != len(chat_ids):
        raise ValueError("projects sharing one Telegram bot must use unique alert_chat_id values")
    by_chat = {cfg.telegram.alert_chat_id: cfg for cfg in configs}
    results: list[tuple[str, CoordinatorResult]] = []
    for update in _extract_channel_posts(payload):
        target_config = by_chat.get(update.chat_id)
        if target_config is None:
            continue
        target_offset = offset_by_chat.get(update.chat_id)
        if target_offset is not None and update.update_id < target_offset:
            continue
        coordinator = coordinators[target_config.project_slug]
        try:
            result = coordinator.handle_alert(
                platform="telegram",
                chat_id=update.chat_id,
                message_id=update.message_id,
                raw_text=update.text,
                dry_run=dry_run,
            )
        except Exception as exc:
            if not dry_run:
                send_telegram_message(
                    target_config,
                    automation_failure_message(
                        f"telegram:{update.chat_id}/{update.message_id}",
                        str(exc),
                        target_config.messages.prefix,
                    ),
                )
            raise
        if not dry_run and result.incident_task_id and result.channel_message:
            send_telegram_message(target_config, result.channel_message)
            status = "correlated" if result.action in {"correlated", "duplicate"} else "acked"
            if result.action in {"recovery_matched", "resolved"}:
                status = "final"
            coordinator.record_channel_delivery(result, status)
        results.append((target_config.project_slug, result))

    if max_update_id is not None and not dry_run:
        new_offset = max_update_id + 1
        for cfg in configs:
            if cfg.telegram.offset_path:
                current_offset = offset_by_chat.get(cfg.telegram.alert_chat_id)
                write_offset(
                    cfg.telegram.offset_path,
                    max(new_offset, current_offset or new_offset),
                )
    return results


def poll_forever(config: AgentConfig, coordinator: AlertCoordinator, dry_run: bool = False) -> None:
    while True:
        poll_once(config, coordinator, dry_run=dry_run)
        time.sleep(config.telegram.poll_interval_seconds)


def send_telegram_message(config: AgentConfig, text: str) -> None:
    _require_bot_token(config)
    response = _telegram_post(
        config, "sendMessage", json={"chat_id": config.telegram.alert_chat_id, "text": text}
    )
    _raise_for_status(response)


def _telegram_url(config: AgentConfig, method: str) -> str:
    return f"https://api.telegram.org/bot{config.telegram.bot_token}/{method}"


def _telegram_get(
    config: AgentConfig, method: str, params: dict[str, str | int]
) -> requests.Response:
    try:
        return requests.get(_telegram_url(config, method), params=params, timeout=30)
    except requests.RequestException as exc:
        raise _sanitized_request_error(method, exc) from None


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
            "before polling or sending Telegram messages"
        )


def _raise_for_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError:
        status = getattr(response, "status_code", "unknown")
        raise RuntimeError(f"Telegram API request failed with HTTP status {status}") from None
