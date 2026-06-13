from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .config import AgentConfig, load_config
from .coordinator import AlertCoordinator
from .kanban import HermesKanbanCliClient
from .ledger import AlertLedger
from .telegram_ingest import poll_forever, poll_once, poll_once_many, send_telegram_message
from .watchdog import WatchdogPolicy, evaluate_stalled_incidents


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-alert-monitor")
    parser.add_argument("--config", required=True, help="Path to config.yaml")
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
    base = load_config(cfg_path, env=env, project_slug=args.project)
    if args.project:
        return [base]
    return [load_config(cfg_path, env=env, project_slug=project.slug) for project in base.projects]


def _telegram_group_key(cfg: AgentConfig) -> str:
    return cfg.telegram.bot_token or f"env:{cfg.telegram.bot_token_env}"


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
                next_due[cfg.project_slug] = now + cfg.telegram.poll_interval_seconds
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
        message_prefix=cfg.messages.prefix,
    )
    rows: list[dict[str, object]] = []
    for finding in findings:
        if args.send_telegram:
            send_telegram_message(cfg, finding.message)
            incident = ledger.get_incident(finding.incident_task_id)
            ledger.update_incident_status(
                finding.incident_task_id,
                status=incident.status if incident else "investigating",
                last_channel_status="watchdog-stalled",
            )
        row = dict(finding.__dict__)
        row["project"] = cfg.project_slug
        rows.append(row)
    return rows


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = Path(args.config)

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
        results = _poll_once_configs(_project_configs(args, cfg_path, env), args)
        print(json.dumps(results, indent=2, sort_keys=True))
        return 0

    if args.command == "listen":
        configs = _project_configs(args, cfg_path, env)
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
        ledger.update_incident_status(args.incident, args.status, args.last_channel_status)
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
