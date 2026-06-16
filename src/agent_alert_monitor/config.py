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
class TelegramSourceConfig:
    name: str
    telegram: TelegramConfig
    type: str = "telegram"


@dataclass(frozen=True)
class AwsSqsSourceConfig:
    name: str
    queue_url_env: str
    queue_url: str
    region: str
    envelope: str
    type: str = "aws_sqs"
    wait_time_seconds: int = 20
    max_messages: int = 10
    visibility_timeout_seconds: int = 300
    delete_policy: str = "after_successful_side_effects"


AlertSourceConfig = AwsSqsSourceConfig | TelegramSourceConfig


@dataclass(frozen=True)
class TelegramSinkConfig:
    name: str
    bot_token: str | None
    chat_id: str
    bot_token_env: str = "ALERT_MONITOR_TELEGRAM_BOT_TOKEN"
    chat_id_env: str | None = None
    type: str = "telegram"


AlertSinkConfig = TelegramSinkConfig


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
    coder_assignee: str | None = None
    reviewer_assignee: str | None = None


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
    environment: str | None = None
    sources: tuple[AlertSourceConfig, ...] = ()
    sinks: tuple[AlertSinkConfig, ...] = ()

    @property
    def telegram_source(self) -> TelegramSourceConfig | None:
        return next(
            (source for source in self.sources if isinstance(source, TelegramSourceConfig)), None
        )

    @property
    def telegram_sink(self) -> TelegramSinkConfig | None:
        return next((sink for sink in self.sinks if isinstance(sink, TelegramSinkConfig)), None)


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


def _telegram_config_from_data(
    telegram_data: dict[str, Any],
    *,
    env_map: Mapping[str, str],
    runtime: RuntimeConfig,
    config_dir: Path,
    slug: str,
    legacy_default: bool,
    strict_env: bool,
) -> TelegramConfig:
    token_env = str(telegram_data.get("bot_token_env", "ALERT_MONITOR_TELEGRAM_BOT_TOKEN"))
    bot_token = str(telegram_data.get("bot_token") or env_map.get(token_env) or "")
    alert_chat_id = str(telegram_data.get("alert_chat_id", ""))
    if not alert_chat_id and strict_env:
        raise ValueError(f"missing projects[{slug}].telegram.alert_chat_id")

    offset_path_value = telegram_data.get("offset_path")
    if offset_path_value:
        offset_path = _resolve_path(offset_path_value, config_dir=config_dir)
    elif legacy_default:
        offset_path = runtime.state_dir / "telegram-offset.json"
    else:
        offset_path = runtime.state_dir / f"{slug}-telegram-offset.json"

    return TelegramConfig(
        bot_token=bot_token,
        alert_chat_id=alert_chat_id,
        bot_token_env=token_env,
        poll_interval_seconds=_int_setting(
            telegram_data, "poll_interval_seconds", 5, strict_env=strict_env
        ),
        offset_path=offset_path,
    )


def _telegram_config_from_sink(
    sink: TelegramSinkConfig,
    *,
    runtime: RuntimeConfig,
    slug: str,
) -> TelegramConfig:
    return TelegramConfig(
        bot_token=sink.bot_token,
        alert_chat_id=sink.chat_id,
        bot_token_env=sink.bot_token_env,
        poll_interval_seconds=5,
        offset_path=runtime.state_dir / f"{slug}-telegram-offset.json",
    )


def _parse_sources(
    project_data: dict[str, Any],
    *,
    env_map: Mapping[str, str],
    slug: str,
    legacy_telegram: TelegramConfig | None,
    strict_env: bool,
) -> tuple[AlertSourceConfig, ...]:
    sources_data = project_data.get("sources")
    if sources_data is None:
        if legacy_telegram is None:
            return ()
        return (TelegramSourceConfig(name=f"{slug}-telegram", telegram=legacy_telegram),)
    if not isinstance(sources_data, list):
        raise ValueError(f"projects[{slug}].sources must be a list")

    sources: list[AlertSourceConfig] = []
    for index, source_data in enumerate(sources_data):
        if not isinstance(source_data, dict):
            raise ValueError(f"projects[{slug}].sources[{index}] must be a mapping")
        source_type = str(source_data.get("type", "")).strip()
        name = str(source_data.get("name") or f"{slug}-{source_type or 'source'}-{index + 1}")
        if source_type == "aws_sqs":
            queue_url_env = str(source_data.get("queue_url_env", ""))
            queue_url = str(source_data.get("queue_url") or env_map.get(queue_url_env, ""))
            region = str(source_data.get("region", ""))
            envelope = str(source_data.get("envelope", ""))
            if strict_env:
                if not queue_url_env and not queue_url:
                    raise ValueError(f"projects[{slug}].sources[{index}].queue_url_env is required")
                if not queue_url:
                    raise ValueError(
                        "missing queue URL environment variable for "
                        f"projects[{slug}].sources[{index}]: {queue_url_env}"
                    )
                if not region:
                    raise ValueError(f"projects[{slug}].sources[{index}].region is required")
                if envelope not in {
                    "aws_sns_cloudwatch_alarm",
                    "aws_eventbridge_cloudwatch_alarm",
                }:
                    raise ValueError(
                        f"projects[{slug}].sources[{index}].envelope must be "
                        "aws_sns_cloudwatch_alarm or aws_eventbridge_cloudwatch_alarm"
                    )
            sources.append(
                AwsSqsSourceConfig(
                    name=name,
                    queue_url_env=queue_url_env,
                    queue_url=queue_url,
                    region=region,
                    envelope=envelope,
                    wait_time_seconds=_int_setting(
                        source_data, "wait_time_seconds", 20, strict_env=strict_env
                    ),
                    max_messages=_int_setting(
                        source_data, "max_messages", 10, strict_env=strict_env
                    ),
                    visibility_timeout_seconds=_int_setting(
                        source_data, "visibility_timeout_seconds", 300, strict_env=strict_env
                    ),
                    delete_policy=str(
                        source_data.get("delete_policy", "after_successful_side_effects")
                    ),
                )
            )
        elif source_type == "telegram":
            if legacy_telegram is None:
                raise ValueError(
                    f"projects[{slug}].sources[{index}] type=telegram requires "
                    f"projects[{slug}].telegram"
                )
            sources.append(TelegramSourceConfig(name=name, telegram=legacy_telegram))
        else:
            raise ValueError(f"unsupported projects[{slug}].sources[{index}].type: {source_type}")
    return tuple(sources)


def _parse_sinks(
    project_data: dict[str, Any],
    *,
    env_map: Mapping[str, str],
    slug: str,
    legacy_telegram: TelegramConfig | None,
    strict_env: bool,
) -> tuple[AlertSinkConfig, ...]:
    sinks_data = project_data.get("sinks")
    if sinks_data is None:
        if legacy_telegram is None:
            return ()
        return (
            TelegramSinkConfig(
                name=f"{slug}-telegram",
                bot_token=legacy_telegram.bot_token,
                bot_token_env=legacy_telegram.bot_token_env,
                chat_id=legacy_telegram.alert_chat_id,
            ),
        )
    if not isinstance(sinks_data, list):
        raise ValueError(f"projects[{slug}].sinks must be a list")

    sinks: list[AlertSinkConfig] = []
    for index, sink_data in enumerate(sinks_data):
        if not isinstance(sink_data, dict):
            raise ValueError(f"projects[{slug}].sinks[{index}] must be a mapping")
        sink_type = str(sink_data.get("type", "")).strip()
        name = str(sink_data.get("name") or f"{slug}-{sink_type or 'sink'}-{index + 1}")
        if sink_type != "telegram":
            raise ValueError(f"unsupported projects[{slug}].sinks[{index}].type: {sink_type}")
        token_env = str(sink_data.get("bot_token_env", "ALERT_MONITOR_TELEGRAM_BOT_TOKEN"))
        bot_token = str(sink_data.get("bot_token") or env_map.get(token_env) or "")
        chat_id_env = sink_data.get("chat_id_env")
        chat_id_env_str = str(chat_id_env) if chat_id_env else None
        chat_id = str(
            sink_data.get("chat_id")
            or (env_map.get(chat_id_env_str, "") if chat_id_env_str else "")
        )
        if not chat_id and strict_env:
            if chat_id_env_str:
                raise ValueError(
                    f"missing chat id environment variable for projects[{slug}].sinks[{index}]: "
                    f"{chat_id_env_str}"
                )
            raise ValueError(f"projects[{slug}].sinks[{index}].chat_id or chat_id_env is required")
        sinks.append(
            TelegramSinkConfig(
                name=name,
                bot_token=bot_token,
                bot_token_env=token_env,
                chat_id=chat_id,
                chat_id_env=chat_id_env_str,
            )
        )
    return tuple(sinks)


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

    telegram_section = project_data.get("telegram")
    if telegram_section is not None and not isinstance(telegram_section, dict):
        raise ValueError(f"config section must be a mapping: projects[{slug}].telegram")
    legacy_telegram = (
        _telegram_config_from_data(
            telegram_section,
            env_map=env_map,
            runtime=runtime,
            config_dir=config_dir,
            slug=slug,
            legacy_default=legacy_default,
            strict_env=strict_env,
        )
        if telegram_section is not None
        else None
    )

    hermes_data = _required_section(project_data, "hermes")
    kanban_data = _required_section(project_data, "kanban")
    messages_data = _optional_section(project_data, "messages")
    sources = _parse_sources(
        project_data,
        env_map=env_map,
        slug=slug,
        legacy_telegram=legacy_telegram,
        strict_env=strict_env,
    )
    sinks = _parse_sinks(
        project_data,
        env_map=env_map,
        slug=slug,
        legacy_telegram=legacy_telegram,
        strict_env=strict_env,
    )
    if legacy_telegram is None and not sources and strict_env:
        raise ValueError(f"projects[{slug}] requires telegram config or explicit sources")
    explicit_sinks = project_data.get("sinks") is not None
    telegram = (
        _telegram_config_from_sink(sinks[0], runtime=runtime, slug=slug)
        if explicit_sinks and sinks
        else legacy_telegram
    )

    return ProjectConfig(
        slug=slug,
        display_name=display_name,
        environment=project_data.get("environment"),
        telegram=telegram
        or TelegramConfig(
            bot_token=None,
            alert_chat_id="",
            offset_path=runtime.state_dir / f"{slug}-telegram-offset.json",
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
            coder_assignee=kanban_data.get("coder_assignee"),
            reviewer_assignee=kanban_data.get("reviewer_assignee"),
        ),
        messages=MessageConfig(prefix=str(messages_data.get("prefix", "Alert monitor"))),
        sources=sources,
        sinks=sinks,
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
    *,
    allow_unresolved_default_project: bool = False,
) -> AgentConfig:
    env_map = dict(os.environ if env is None else env)
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")

    top_level_source = {
        key: value for key, value in data.items() if key not in {"projects", "default_project"}
    }
    top_level = _expand_env(top_level_source, env_map, strict=True)
    runtime = _runtime_config(top_level, config_dir=path.parent)
    watchdog = _watchdog_config(top_level)
    has_projects_section = data.get("projects") is not None
    entries = _project_entries(data)
    if project_slug is not None:
        selected_slug = project_slug
    else:
        raw_default_project = data.get("default_project")
        try:
            selected_slug = str(
                _expand_env(raw_default_project, env_map, strict=True)
                if raw_default_project is not None
                else _expand_env(entries[0].get("slug") or "default", env_map, strict=True)
            )
        except ValueError:
            if not has_projects_section or not allow_unresolved_default_project:
                raise
            selected_slug = str(
                _expand_env(entries[0].get("slug") or "default", env_map, strict=True)
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
