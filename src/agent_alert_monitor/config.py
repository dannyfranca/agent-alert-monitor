from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str | None
    alert_chat_id: str
    bot_token_env: str = "ALERT_MONITOR_TELEGRAM_BOT_TOKEN"
    poll_interval_seconds: int = 5
    offset_path: Path | None = None


@dataclass(frozen=True)
class HermesConfig:
    coordinator_profile: str
    kanban_board: str | None = None
    channel_target: str | None = None


@dataclass(frozen=True)
class KanbanConfig:
    incident_assignee: str
    default_priority: int = 1000
    critical_priority: int = 2000
    tenant: str = "application"


@dataclass(frozen=True)
class RuntimeConfig:
    state_dir: Path
    ledger_path: Path


@dataclass(frozen=True)
class WatchdogConfig:
    ack_sla_seconds: int = 120
    progress_sla_seconds: int = 600
    stalled_after_seconds: int = 900


@dataclass(frozen=True)
class MessageConfig:
    prefix: str = "Alert monitor"


@dataclass(frozen=True)
class ProjectConfig:
    slug: str
    display_name: str
    telegram: TelegramConfig
    hermes: HermesConfig
    kanban: KanbanConfig
    messages: MessageConfig = field(default_factory=MessageConfig)


@dataclass(frozen=True)
class AgentConfig:
    telegram: TelegramConfig
    hermes: HermesConfig
    kanban: KanbanConfig
    runtime: RuntimeConfig
    watchdog: WatchdogConfig
    project_slug: str = "default"
    project_display_name: str = "Application"
    messages: MessageConfig = field(default_factory=MessageConfig)
    projects: tuple[ProjectConfig, ...] = ()

    @property
    def project(self) -> ProjectConfig:
        if self.projects:
            for project in self.projects:
                if project.slug == self.project_slug:
                    return project
        return ProjectConfig(
            slug=self.project_slug,
            display_name=self.project_display_name,
            telegram=self.telegram,
            hermes=self.hermes,
            kanban=self.kanban,
            messages=self.messages,
        )


def _expand_env(value: Any, env: Mapping[str, str], *, strict: bool = True) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env or not env[name]:
                if not strict:
                    return match.group(0)
                raise ValueError(f"missing environment variable referenced by config: {name}")
            return env[name]

        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(v, env, strict=strict) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v, env, strict=strict) for k, v in value.items()}
    return value


def _required_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name)
    if not isinstance(section, dict):
        raise ValueError(f"missing required config section: {name}")
    return section


def _optional_section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name) or {}
    if not isinstance(section, dict):
        raise ValueError(f"config section must be a mapping: {name}")
    return section


def _resolve_path(path_value: object, *, config_dir: Path) -> Path:
    path = Path(str(path_value)).expanduser()
    if not path.is_absolute():
        path = (config_dir / path).resolve()
    return path


def _int_setting(data: dict[str, Any], key: str, default: int, *, strict_env: bool) -> int:
    value = data.get(key, default)
    if isinstance(value, str) and _ENV_PATTERN.search(value) and not strict_env:
        return default
    return int(value)


def _runtime_config(data: dict[str, Any], *, config_dir: Path) -> RuntimeConfig:
    runtime_data = _required_section(data, "runtime")
    state_dir = _resolve_path(runtime_data.get("state_dir", "./state"), config_dir=config_dir)
    ledger_path = _resolve_path(
        runtime_data.get("ledger_path", state_dir / "ledger.sqlite"),
        config_dir=config_dir,
    )
    return RuntimeConfig(state_dir=state_dir, ledger_path=ledger_path)


def _watchdog_config(data: dict[str, Any]) -> WatchdogConfig:
    watchdog_data = data.get("watchdog") or {}
    if not isinstance(watchdog_data, dict):
        raise ValueError("config section must be a mapping: watchdog")
    return WatchdogConfig(
        ack_sla_seconds=int(watchdog_data.get("ack_sla_seconds", 120)),
        progress_sla_seconds=int(watchdog_data.get("progress_sla_seconds", 600)),
        stalled_after_seconds=int(watchdog_data.get("stalled_after_seconds", 900)),
    )


def _parse_project(
    project_data: dict[str, Any],
    *,
    env_map: Mapping[str, str],
    runtime: RuntimeConfig,
    config_dir: Path,
    legacy_default: bool = False,
    strict_env: bool = True,
) -> ProjectConfig:
    project_data = _expand_env(project_data, env_map, strict=strict_env)
    slug = str(project_data.get("slug") or ("default" if legacy_default else "")).strip()
    if not slug:
        raise ValueError("project slug is required")
    display_name = str(project_data.get("display_name") or slug).strip()

    telegram_data = _required_section(project_data, "telegram")
    hermes_data = _required_section(project_data, "hermes")
    kanban_data = _required_section(project_data, "kanban")
    messages_data = _optional_section(project_data, "messages")

    token_env = str(telegram_data.get("bot_token_env", "ALERT_MONITOR_TELEGRAM_BOT_TOKEN"))
    bot_token = str(telegram_data.get("bot_token") or env_map.get(token_env) or "")
    alert_chat_id = str(telegram_data.get("alert_chat_id", ""))
    if not alert_chat_id:
        raise ValueError(f"missing projects[{slug}].telegram.alert_chat_id")

    offset_path_value = telegram_data.get("offset_path")
    if offset_path_value:
        offset_path = _resolve_path(offset_path_value, config_dir=config_dir)
    elif legacy_default:
        offset_path = runtime.state_dir / "telegram-offset.json"
    else:
        offset_path = runtime.state_dir / f"{slug}-telegram-offset.json"

    return ProjectConfig(
        slug=slug,
        display_name=display_name,
        telegram=TelegramConfig(
            bot_token=bot_token,
            alert_chat_id=alert_chat_id,
            bot_token_env=token_env,
            poll_interval_seconds=_int_setting(
                telegram_data, "poll_interval_seconds", 5, strict_env=strict_env
            ),
            offset_path=offset_path,
        ),
        hermes=HermesConfig(
            coordinator_profile=str(hermes_data.get("coordinator_profile", "alert-coordinator")),
            kanban_board=hermes_data.get("kanban_board"),
            channel_target=hermes_data.get("channel_target"),
        ),
        kanban=KanbanConfig(
            incident_assignee=str(kanban_data.get("incident_assignee", "debugger")),
            default_priority=_int_setting(
                kanban_data, "default_priority", 1000, strict_env=strict_env
            ),
            critical_priority=_int_setting(
                kanban_data, "critical_priority", 2000, strict_env=strict_env
            ),
            tenant=str(kanban_data.get("tenant", slug if not legacy_default else "application")),
        ),
        messages=MessageConfig(prefix=str(messages_data.get("prefix", "Alert monitor"))),
    )


def _project_entries(data: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    projects_data = data.get("projects")
    if projects_data is None:
        return (
            {
                "slug": data.get("project_slug", "default"),
                "display_name": data.get("project_display_name", "Application"),
                "telegram": _required_section(data, "telegram"),
                "hermes": _required_section(data, "hermes"),
                "kanban": _required_section(data, "kanban"),
                "messages": data.get("messages") or {},
            },
        )
    if not isinstance(projects_data, list) or not projects_data:
        raise ValueError("projects must be a non-empty list")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(projects_data):
        if not isinstance(item, dict):
            raise ValueError(f"projects[{index}] must be a mapping")
        entries.append(item)
    return tuple(entries)


def load_config(
    path: str | Path,
    env: Mapping[str, str] | None = None,
    project_slug: str | None = None,
) -> AgentConfig:
    env_map = dict(os.environ if env is None else env)
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")

    top_level = _expand_env(
        {key: value for key, value in data.items() if key != "projects"},
        env_map,
        strict=True,
    )
    runtime = _runtime_config(top_level, config_dir=path.parent)
    watchdog = _watchdog_config(top_level)
    has_projects_section = data.get("projects") is not None
    entries = _project_entries(data)
    selected_slug = project_slug or str(
        top_level.get("default_project")
        or _expand_env(entries[0].get("slug") or "default", env_map, strict=True)
    )
    def entry_matches_selected(item: dict[str, Any]) -> bool:
        raw_slug = item.get("slug", "default")
        try:
            item_slug = str(_expand_env(raw_slug, env_map, strict=True))
        except ValueError:
            item_slug = str(raw_slug)
        return item_slug == selected_slug

    projects = tuple(
        _parse_project(
            item,
            env_map=env_map,
            runtime=runtime,
            config_dir=path.parent,
            legacy_default=not has_projects_section,
            strict_env=not has_projects_section or entry_matches_selected(item),
        )
        for item in entries
    )
    slugs = [project.slug for project in projects]
    if len(set(slugs)) != len(slugs):
        raise ValueError("project slugs must be unique")

    selected = next((project for project in projects if project.slug == selected_slug), None)
    if selected is None:
        available = ", ".join(slugs)
        raise ValueError(f"unknown project slug: {selected_slug}; available projects: {available}")

    return AgentConfig(
        telegram=selected.telegram,
        hermes=selected.hermes,
        kanban=selected.kanban,
        runtime=runtime,
        watchdog=watchdog,
        project_slug=selected.slug,
        project_display_name=selected.display_name,
        messages=selected.messages,
        projects=projects,
    )
