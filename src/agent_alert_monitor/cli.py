from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from .config import AgentConfig, load_config
from .coordinator import AlertCoordinator
from .kanban import HermesKanbanCliClient
from .ledger import AlertLedger
from .telegram_ingest import poll_forever, poll_once, poll_once_many, send_telegram_message
from .watchdog import WatchdogPolicy, evaluate_stalled_incidents

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-alert-monitor")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--project",
        help=(
            "Project slug from config.yaml. Ingest/listen/watchdog process all "
            "projects when omitted."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    synthetic = sub.add_parser(
        "synthetic-alert", help="Run a local synthetic alert through the coordinator"
    )
    synthetic.add_argument("--message-id", default="synthetic-1")
    synthetic.add_argument("--chat-id")
    synthetic.add_argument("--text", required=True)
    synthetic.add_argument("--dry-run", action="store_true", default=False)

    ingest = sub.add_parser("ingest", help="Poll Telegram once and ingest matching channel posts")
    ingest.add_argument("--dry-run", action="store_true", default=False)

    listen = sub.add_parser("listen", help="Continuously poll Telegram for alert channel posts")
    listen.add_argument("--dry-run", action="store_true", default=False)

    incident = sub.add_parser("incident-update", help="Update local incident status in the ledger")
    incident.add_argument("--incident", required=True)
    incident.add_argument("--status", required=True)
    incident.add_argument("--last-channel-status")

    watchdog = sub.add_parser("watchdog-due", help="Print stalled incidents as JSON")
    watchdog.add_argument("--stalled-after-seconds", type=int)
    watchdog.add_argument("--send-telegram", action="store_true", default=False)

    setup = sub.add_parser("setup", help="Interactive local setup wizard")
    setup.add_argument("--root", default=".", help="Repo/runtime root for config.yaml and .env")
    setup.add_argument("--skip-live-checks", action="store_true")
    setup.add_argument("--force", action="store_true", help="Overwrite existing config.yaml/.env")

    return parser


def _coordinator_for_config(
    args: argparse.Namespace, cfg: AgentConfig
) -> tuple[AlertLedger | None, AlertCoordinator]:
    dry_run = bool(getattr(args, "dry_run", False))
    ledger = (
        None
        if dry_run and args.command in {"synthetic-alert", "ingest", "listen"}
        else AlertLedger(cfg.runtime.ledger_path)
    )
    kanban_client = (
        None
        if dry_run
        else HermesKanbanCliClient(
            profile=cfg.hermes.coordinator_profile,
            board=cfg.hermes.kanban_board,
        )
    )
    return ledger, AlertCoordinator(cfg, ledger=ledger, kanban_client=kanban_client)


def _coordinator_for_args(
    args: argparse.Namespace, cfg_path: Path, env: Mapping[str, str] | None
) -> tuple[AgentConfig, AlertLedger | None, AlertCoordinator]:
    cfg = load_config(cfg_path, env=env, project_slug=args.project)
    ledger, coordinator = _coordinator_for_config(args, cfg)
    return cfg, ledger, coordinator


def _project_configs(
    args: argparse.Namespace, cfg_path: Path, env: Mapping[str, str] | None
) -> list[AgentConfig]:
    base = load_config(
        cfg_path,
        env=env,
        project_slug=args.project,
        allow_unresolved_default_project=args.project is None,
    )
    if args.project:
        return [base]
    return [load_config(cfg_path, env=env, project_slug=project.slug) for project in base.projects]


def _telegram_intake_configs(configs: list[AgentConfig]) -> list[AgentConfig]:
    return [cfg for cfg in configs if cfg.project.telegram_source is not None]


def _raw_config_data(cfg_path: Path) -> dict[str, object]:
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def _project_entry_has_telegram_source(project: object) -> bool:
    if not isinstance(project, dict):
        return False
    if "sources" not in project:
        return isinstance(project.get("telegram"), dict)
    sources = project.get("sources")
    if not isinstance(sources, list):
        raise ValueError("projects[].sources must be a list")
    has_telegram = False
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("projects[].sources[] must be a mapping")
        source_type = source.get("type")
        if source_type == "telegram":
            has_telegram = True
        elif source_type != "aws_sqs":
            raise ValueError(f"unsupported projects[].sources[].type: {source_type}")
    return has_telegram


def _expand_raw_slug(raw_slug: object, env: Mapping[str, str] | None) -> str:
    env_map = os.environ if env is None else env

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return env_map.get(name, match.group(0))

    return _ENV_PATTERN.sub(replace, str(raw_slug or "default"))


def _project_entry_slug(project: dict[str, object], env: Mapping[str, str] | None) -> str:
    return _expand_raw_slug(project.get("slug"), env)


def _telegram_intake_project_configs(
    args: argparse.Namespace, cfg_path: Path, env: Mapping[str, str] | None
) -> list[AgentConfig]:
    data = _raw_config_data(cfg_path)
    projects = data.get("projects")
    if projects is None:
        if not isinstance(data.get("telegram"), dict):
            return []
        return _project_configs(args, cfg_path, env)
    if not isinstance(projects, list):
        raise ValueError("projects must be a non-empty list")
    if args.project:
        selected = next(
            (
                project
                for project in projects
                if isinstance(project, dict) and _project_entry_slug(project, env) == args.project
            ),
            None,
        )
        if selected is None:
            load_config(cfg_path, env=env, project_slug=args.project)
        if not _project_entry_has_telegram_source(selected):
            return []
        cfg = load_config(cfg_path, env=env, project_slug=args.project)
        return [cfg]
    slugs = [
        _project_entry_slug(project, env)
        for project in projects
        if _project_entry_has_telegram_source(project)
    ]
    return [load_config(cfg_path, env=env, project_slug=slug) for slug in slugs]


def _telegram_group_key(cfg: AgentConfig) -> str:
    telegram = cfg.project.telegram_source.telegram if cfg.project.telegram_source else cfg.telegram
    return telegram.bot_token or f"env:{telegram.bot_token_env}"


def _incident_scope_for_config(cfg: AgentConfig) -> str:
    if cfg.project_slug == "default" and not cfg.hermes.kanban_board:
        return "default"
    board = cfg.hermes.kanban_board or "default"
    return "|".join(
        [
            f"project:{cfg.project_slug}",
            f"profile:{cfg.hermes.coordinator_profile}",
            f"board:{board}",
        ]
    )


def _poll_once_configs(
    configs: list[AgentConfig], args: argparse.Namespace
) -> list[dict[str, object]]:
    coordinators = {cfg.project_slug: _coordinator_for_config(args, cfg)[1] for cfg in configs}
    grouped: dict[str, list[AgentConfig]] = {}
    for cfg in configs:
        grouped.setdefault(_telegram_group_key(cfg), []).append(cfg)

    rows: list[dict[str, object]] = []
    for group in grouped.values():
        if len(group) == 1:
            cfg = group[0]
            for result in poll_once(cfg, coordinators[cfg.project_slug], dry_run=args.dry_run):
                row = result.as_dict()
                row["project"] = cfg.project_slug
                rows.append(row)
            continue
        for project_slug, result in poll_once_many(group, coordinators, dry_run=args.dry_run):
            row = result.as_dict()
            row["project"] = project_slug
            rows.append(row)
    return rows


def _poll_forever_many(configs: list[AgentConfig], args: argparse.Namespace) -> None:
    next_due = {cfg.project_slug: 0.0 for cfg in configs}
    while True:
        now = time.monotonic()
        due = [cfg for cfg in configs if now >= next_due[cfg.project_slug]]
        if due:
            due_keys = {_telegram_group_key(cfg) for cfg in due}
            poll_configs = [cfg for cfg in configs if _telegram_group_key(cfg) in due_keys]
            _poll_once_configs(poll_configs, args)
            for cfg in poll_configs:
                source = cfg.project.telegram_source
                interval = (
                    source.telegram.poll_interval_seconds
                    if source
                    else cfg.telegram.poll_interval_seconds
                )
                next_due[cfg.project_slug] = now + interval
        sleep_for = max(1.0, min(next_due.values()) - time.monotonic())
        time.sleep(sleep_for)


def _watchdog_findings_for_config(
    cfg: AgentConfig, args: argparse.Namespace
) -> list[dict[str, object]]:
    ledger = AlertLedger(cfg.runtime.ledger_path)
    policy = WatchdogPolicy(
        ack_sla_seconds=cfg.watchdog.ack_sla_seconds,
        progress_sla_seconds=cfg.watchdog.progress_sla_seconds,
        stalled_after_seconds=args.stalled_after_seconds or cfg.watchdog.stalled_after_seconds,
    )
    findings = evaluate_stalled_incidents(
        ledger,
        policy=policy,
        project_slug=cfg.project_slug,
        incident_scope=_incident_scope_for_config(cfg),
        message_prefix=cfg.messages.prefix,
    )
    rows: list[dict[str, object]] = []
    for finding in findings:
        if args.send_telegram:
            send_telegram_message(cfg, finding.message)
            incident = ledger.get_incident(
                finding.incident_task_id, incident_scope=finding.incident_scope
            )
            ledger.update_incident_status(
                finding.incident_task_id,
                status=incident.status if incident else "investigating",
                last_channel_status="watchdog-stalled",
                incident_scope=finding.incident_scope,
            )
        row = dict(finding.__dict__)
        row["project"] = cfg.project_slug
        rows.append(row)
    return rows


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config)

    if args.command == "setup":
        from . import setup_wizard

        setup_result = setup_wizard.run_setup_wizard(
            root=Path(args.root),
            validate_live=not args.skip_live_checks,
            force=args.force,
        )
        return 0 if setup_result.checks_failed == 0 else 1

    if args.command == "synthetic-alert":
        cfg, _ledger, coordinator = _coordinator_for_args(args, cfg_path, env)
        result = coordinator.handle_alert(
            platform="telegram",
            chat_id=args.chat_id or cfg.telegram.alert_chat_id,
            message_id=args.message_id,
            raw_text=args.text,
            dry_run=args.dry_run,
        )
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "ingest":
        configs = _telegram_intake_project_configs(args, cfg_path, env)
        results = _poll_once_configs(configs, args) if configs else []
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0

    if args.command == "listen":
        configs = _telegram_intake_project_configs(args, cfg_path, env)
        if not configs:
            raise ValueError("no projects with Telegram sources are configured for listen")
        if len(configs) == 1:
            cfg = configs[0]
            _ledger, coordinator = _coordinator_for_config(args, cfg)
            poll_forever(cfg, coordinator, dry_run=args.dry_run)
        else:
            _poll_forever_many(configs, args)
        return 0

    if args.command == "incident-update":
        cfg, ledger, _coordinator = _coordinator_for_args(args, cfg_path, env)
        if ledger is None:
            raise RuntimeError("incident-update requires a ledger")
        ledger.update_incident_status(
            args.incident,
            args.status,
            args.last_channel_status,
            incident_scope=_incident_scope_for_config(cfg),
        )
        print(
            json.dumps(
                {"project": cfg.project_slug, "updated": args.incident, "status": args.status},
                sort_keys=True,
            )
        )
        return 0

    if args.command == "watchdog-due":
        rows: list[dict[str, object]] = []
        for cfg in _project_configs(args, cfg_path, env):
            rows.extend(_watchdog_findings_for_config(cfg, args))
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
