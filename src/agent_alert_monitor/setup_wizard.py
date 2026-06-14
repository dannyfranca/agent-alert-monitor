from __future__ import annotations

import getpass
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import requests
import yaml


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class SetupResult:
    config_path: Path
    env_path: Path
    checks_failed: int
    messages: list[str] = field(default_factory=list)


class TelegramResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> dict[str, object]: ...


InputFn = Callable[[str], str]
SecretFn = Callable[[str], str]
PrintFn = Callable[[str], None]
CommandRunner = Callable[[Sequence[str], int], CommandResult]
TelegramGet = Callable[[str, dict[str, str | int] | None, int], TelegramResponse]


def run_command(command: Sequence[str], timeout: int = 30) -> CommandResult:
    try:
        proc = subprocess.run(
            list(command), capture_output=True, check=False, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", str(exc))
    except PermissionError as exc:
        return CommandResult(126, "", str(exc))
    except OSError as exc:
        return CommandResult(126, "", str(exc))
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"command timed out after {timeout}s")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def requests_get(url: str, params: dict[str, str | int] | None = None, timeout: int = 15):
    return requests.get(url, params=params, timeout=timeout)


def run_setup_wizard(
    *,
    root: Path,
    input_fn: InputFn = input,
    secret_fn: SecretFn = getpass.getpass,
    print_fn: PrintFn = print,
    command_runner: CommandRunner = run_command,
    telegram_get: TelegramGet = requests_get,
    validate_live: bool = True,
    force: bool = False,
) -> SetupResult:
    root = root.resolve()
    config_path = root / "config.yaml"
    env_path = root / ".env"
    messages: list[str] = []

    if not force:
        existing = [str(path.name) for path in (config_path, env_path) if path.exists()]
        if existing:
            message = (
                "Local setup file already exists; refusing to overwrite: "
                + ", ".join(existing)
                + ". Re-run with --force if you intentionally want to replace them."
            )
            print_fn(f"❌ {message}")
            return SetupResult(config_path, env_path, 1, [message])

    print_fn("Agent Alert Monitor interactive setup")
    print_fn("You will need:")
    print_fn("- A Telegram listener bot token from @BotFather (/newbot).")
    print_fn("- The listener bot; add it as an admin to each alert channel.")
    print_fn(
        "- Each alert channel id, usually -100...; discover it through the listener "
        "bot getUpdates flow."
    )
    print_fn(
        "- Hermes profiles/boards ready, or permission to create them with the commands "
        "shown below."
    )
    print_fn("")

    state_dir = _ask(input_fn, "State directory", "./state")
    try:
        project_count = int(_ask(input_fn, "How many alert projects/channels?", "1"))
    except ValueError:
        message = "Project count must be a number."
        print_fn(f"❌ {message}")
        return SetupResult(config_path, env_path, 1, [message])
    if project_count < 1:
        message = "Setup requires at least one project/channel."
        print_fn(f"❌ {message}")
        return SetupResult(config_path, env_path, 1, [message])
    projects: list[dict[str, Any]] = []
    state_path = _resolve_state_dir(root, state_dir)
    if state_path.exists() and not state_path.is_dir():
        message = f"State path exists but is not a directory: {state_path}"
        print_fn(f"❌ {message}")
        return SetupResult(config_path, env_path, 1, [message])
    if state_path.exists() and state_path.is_dir():
        mode = state_path.stat().st_mode & 0o777
        if mode & 0o077:
            message = (
                f"State directory permissions are too open: {state_path} has mode "
                f"{mode:o}; run chmod 700 on it or choose a private directory."
            )
            print_fn(f"❌ {message}")
            return SetupResult(config_path, env_path, 1, [message])
    env_values: dict[str, str] = {"ALERT_MONITOR_STATE_DIR": str(state_path)}
    checks_failed = 0
    used_slugs: set[str] = set()
    chat_ids_by_token: dict[str, set[str]] = {}

    for index in range(project_count):
        print_fn(f"\nProject {index + 1}/{project_count}")
        display_name = _ask(input_fn, "Project display name", "Sample API")
        while True:
            slug = _ask(input_fn, "Project slug", _slugify(display_name))
            if not _is_slug(slug):
                print_fn("❌ Project slug must use lowercase letters, numbers, and hyphens.")
                continue
            if slug not in used_slugs:
                used_slugs.add(slug)
                break
            print_fn(f"❌ Project slug already used: {slug}. Choose a unique slug.")
        while True:
            token_env = _ask(
                input_fn,
                "Telegram bot token env var",
                f"ALERT_MONITOR_{_env_slug(slug)}_TELEGRAM_BOT_TOKEN",
            )
            if not _is_env_name(token_env):
                print_fn(f"❌ Invalid environment variable name: {token_env}")
                continue
            if token_env in env_values:
                print_fn(f"❌ Environment variable name already used/reserved: {token_env}")
                continue
            break
        print_fn(
            "Telegram token: create a dedicated listener bot with @BotFather (/newbot); "
            "do not reuse the alert-posting bot because bots do not receive their own posts."
        )
        while True:
            token = secret_fn(f"Telegram bot token for {display_name} (input hidden): ")
            if token:
                env_values[token_env] = token
                break
            print_fn("❌ Telegram bot token is required.")
        print_fn(
            "Alert chat id: add the listener bot as an admin and post a test alert. "
            "If you leave the next prompt blank, the wizard will call getUpdates with "
            "the hidden token and print candidate chat ids without exposing the token."
        )
        alert_chat_id = input_fn(
            "Telegram alert channel id [leave blank to list recent chats]: "
        ).strip()
        if not alert_chat_id:
            _print_recent_chat_ids(print_fn, telegram_get, token)
            alert_chat_id = _ask(input_fn, "Telegram alert channel id", "-1001234567890")
        while alert_chat_id in chat_ids_by_token.setdefault(token, set()):
            print_fn(
                "❌ Projects sharing one Telegram bot token must use unique alert chat ids."
            )
            alert_chat_id = _ask(input_fn, "Telegram alert channel id", "-1001234567890")
        chat_ids_by_token[token].add(alert_chat_id)
        coordinator_profile = _ask(input_fn, "Hermes coordinator profile", "alert-coordinator")
        kanban_board = _ask(input_fn, "Hermes Kanban board slug", f"{slug}-incidents")
        incident_assignee = _ask(input_fn, "Incident debugger/assignee profile", "debugger")
        tenant = _ask(input_fn, "Kanban tenant", slug)
        default_priority = _ask_int(input_fn, print_fn, "Default incident priority", "1000")
        critical_priority = _ask_int(input_fn, print_fn, "Critical incident priority", "2000")
        message_prefix = _ask(
            input_fn, "Visible Telegram message prefix", f"{display_name} alert monitor"
        )

        print_fn("Hermes setup commands if missing:")
        print_fn(f"  hermes profile create {coordinator_profile}")
        print_fn(f"  hermes -p {coordinator_profile} setup")
        print_fn(f"  hermes -p {coordinator_profile} kanban init")
        print_fn(f"  hermes -p {coordinator_profile} kanban boards create {kanban_board}")
        print_fn(f"  hermes -p {coordinator_profile} gateway install")
        print_fn(f"  hermes -p {coordinator_profile} gateway start")
        print_fn(f"  hermes -p {coordinator_profile} gateway status")
        print_fn(f"  hermes profile create {incident_assignee}")
        print_fn(f"  hermes -p {incident_assignee} setup")

        if validate_live:
            checks_failed += _validate_telegram(print_fn, telegram_get, token, alert_chat_id)
            checks_failed += _validate_hermes(
                print_fn, command_runner, coordinator_profile, incident_assignee, kanban_board
            )

        projects.append(
            {
                "slug": slug,
                "display_name": display_name,
                "telegram": {
                    "bot_token_env": token_env,
                    "alert_chat_id": alert_chat_id,
                    "poll_interval_seconds": 5,
                    "offset_path": f"${{ALERT_MONITOR_STATE_DIR}}/{slug}-telegram-offset.json",
                },
                "hermes": {
                    "coordinator_profile": coordinator_profile,
                    "kanban_board": kanban_board,
                    "channel_target": f"telegram:{alert_chat_id}",
                },
                "kanban": {
                    "incident_assignee": incident_assignee,
                    "tenant": tenant,
                    "default_priority": default_priority,
                    "critical_priority": critical_priority,
                },
                "messages": {"prefix": message_prefix},
            }
        )

    want_aws = _yes_no(
        input_fn, "Do you want AWS readonly credential setup instructions now?", False
    )
    if want_aws:
        print_fn("AWS readonly setup:")
        print_fn(
            "  1. Create an IAM user/role with CloudWatch + CloudWatch Logs "
            "readonly permissions."
        )
        print_fn("  2. Run: ./scripts/setup-aws-readonly.sh")
        print_fn(
            "  3. The script validates sts, cloudwatch describe-alarms, and logs "
            "describe-log-groups."
        )

    config_data: dict[str, object] = {
        "runtime": {
            "state_dir": "${ALERT_MONITOR_STATE_DIR}",
            "ledger_path": "${ALERT_MONITOR_STATE_DIR}/ledger.sqlite",
        },
        "watchdog": {
            "ack_sla_seconds": 120,
            "progress_sla_seconds": 600,
            "stalled_after_seconds": 900,
        },
        "default_project": projects[0]["slug"],
        "projects": projects,
    }

    root.mkdir(parents=True, exist_ok=True)
    _ignore_local_state_dir(root, state_dir)
    state_existed = state_path.exists()
    try:
        state_path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        message = f"Could not create state directory {state_path}: {type(exc).__name__}"
        print_fn(f"❌ {message}")
        return SetupResult(config_path, env_path, 1, [message])
    if not state_existed:
        with suppress(PermissionError):
            state_path.chmod(0o700)

    config_path.write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")
    _write_secret_file(env_path, _render_env(env_values))

    render_systemd = _yes_no(input_fn, "Render systemd user unit files now?", False)
    if render_systemd:
        checks_failed += _run_systemd_install(print_fn, command_runner, root)
    print_fn("Before live start, clear Telegram webhooks and pending test updates:")
    print_fn(f"cd {_shell_env_value(str(root))}")
    print_fn("set -a; . ./.env; set +a")
    print_fn("python - <<'PY'")
    print_fn("import os, urllib.parse, urllib.request")
    for project in projects:
        token_env = project["telegram"]["bot_token_env"]
        print_fn(f"token = os.environ[{token_env!r}]")
        print_fn("base = 'https://api.telegram.org/bot' + token + '/deleteWebhook'")
        print_fn("query = urllib.parse.urlencode({'drop_pending_updates': 'true'})")
        print_fn("urllib.request.urlopen(base + '?' + query, timeout=15).read()")
    print_fn("PY")
    print_fn("Systemd start after dry-run + offset/webhook verification:")
    print_fn("  ./scripts/systemd-install.sh")
    print_fn(
        "  systemctl --user enable --now agent-alert-monitor-ingest.service "
        "agent-alert-monitor-watchdog.timer"
    )

    print_fn("\nWrote local setup files:")
    print_fn(f"- {config_path}")
    print_fn(f"- {env_path} (0600, contains secrets)")
    print_fn("\nNext verification commands:")
    print_fn(f"  cd {_shell_env_value(str(root))}")
    print_fn("  source .venv/bin/activate  # if using the local repo install")
    print_fn("  set -a; . ./.env; set +a")
    for project in projects:
        print_fn(
            "  agent-alert-monitor --config config.yaml --project "
            f"{project['slug']} synthetic-alert --text "
            "'CRITICAL ALARM: Service5xx service=api' --dry-run"
        )
        hermes = project["hermes"]
        kanban = project["kanban"]
        print_fn(
            f"  hermes -p {hermes['coordinator_profile']} kanban --board "
            f"{hermes['kanban_board']} create 'setup smoke incident' "
            f"--assignee {kanban['incident_assignee']} --body 'Verify alert monitor routing.'"
        )
    print_fn("  agent-alert-monitor --config config.yaml ingest --dry-run")

    messages.append("setup completed")
    return SetupResult(config_path, env_path, checks_failed, messages)


def _ask(input_fn: InputFn, label: str, default: str) -> str:
    value = input_fn(f"{label} [{default}]: ").strip()
    return value or default


def _yes_no(input_fn: InputFn, label: str, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input_fn(f"{label} [{suffix}]: ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def _ask_int(input_fn: InputFn, print_fn: PrintFn, label: str, default: str) -> int:
    while True:
        value = _ask(input_fn, label, default)
        try:
            return int(value)
        except ValueError:
            print_fn(f"❌ {label} must be a number.")


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "application"


def _is_slug(value: str) -> bool:
    return re.fullmatch(r"[a-z0-9][a-z0-9-]*", value) is not None


def _env_slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_") or "APPLICATION"


def _is_env_name(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


def _shell_env_value(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _render_env(values: dict[str, str]) -> str:
    lines = ["# Local secrets/config for agent-alert-monitor. Do not commit."]
    for key, value in values.items():
        lines.append(f"{key}={_shell_env_value(value)}")
    return "\n".join(lines) + "\n"


def _write_secret_file(path: Path, content: str) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    finally:
        with suppress(FileNotFoundError):
            tmp_path.unlink()


def _ignore_local_state_dir(root: Path, state_dir: str) -> None:
    path = Path(os.path.expandvars(state_dir)).expanduser()
    if path.is_absolute():
        try:
            normalized = path.resolve().relative_to(root).as_posix().rstrip("/")
        except ValueError:
            return
    else:
        normalized = path.as_posix().removeprefix("./").rstrip("/")
    if normalized in {"state", ""} or normalized.startswith("state-"):
        return
    exclude = _git_info_exclude(root)
    if exclude is None:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    entry = f"/{normalized}/"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if entry not in existing.splitlines():
        with exclude.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(entry + "\n")


def _git_info_exclude(root: Path) -> Path | None:
    git_path = root / ".git"
    if git_path.is_dir():
        return git_path / "info" / "exclude"
    if not git_path.is_file():
        return None
    first_line = git_path.read_text(encoding="utf-8").splitlines()[0]
    if not first_line.startswith("gitdir: "):
        return None
    git_dir = Path(first_line.removeprefix("gitdir: ").strip())
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    return git_dir / "info" / "exclude"


def _resolve_state_dir(root: Path, state_dir: str) -> Path:
    path = Path(os.path.expandvars(state_dir)).expanduser()
    if not path.is_absolute():
        path = root / path
    return path


def _print_recent_chat_ids(
    print_fn: PrintFn, telegram_get: TelegramGet, token: str
) -> None:
    try:
        response = telegram_get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            {"timeout": 0, "allowed_updates": '["channel_post","message"]'},
            15,
        )
        response.raise_for_status()
        payload = response.json()
        updates = payload.get("result")
        if not isinstance(updates, list) or not updates:
            print_fn("No recent Telegram updates found; post a test alert and try again.")
            return
        print_fn("Recent Telegram chat candidates:")
        for item in updates[-10:]:
            if not isinstance(item, dict):
                continue
            post = item.get("channel_post") or item.get("message") or {}
            if not isinstance(post, dict):
                continue
            chat = post.get("chat") or {}
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            title = chat.get("title") or chat.get("username") or chat.get("type") or "unknown"
            print_fn(f"- chat.id={chat['id']} title={title}")
    except Exception as exc:
        print_fn(f"Could not list recent Telegram chats: {_safe_validation_error(exc)}")


def _validate_telegram(
    print_fn: PrintFn,
    telegram_get: TelegramGet,
    token: str,
    alert_chat_id: str,
) -> int:
    if not token:
        print_fn("⚠️ Telegram validation skipped: no token entered.")
        return 1
    base = f"https://api.telegram.org/bot{token}"
    failures = 0
    checks: list[tuple[str, str, dict[str, str | int] | None]] = [
        ("Telegram bot token", "getMe", None),
        ("Telegram alert chat access", "getChat", {"chat_id": alert_chat_id}),
        ("Telegram webhook status", "getWebhookInfo", None),
    ]
    for name, path, params in checks:
        try:
            response = telegram_get(f"{base}/{path}", params, 15)
            response.raise_for_status()
            payload = response.json()
            if payload.get("ok") is False:
                raise RuntimeError(str(payload))
            if path == "getWebhookInfo":
                result = payload.get("result")
                if isinstance(result, dict) and result.get("url"):
                    raise RuntimeError("webhook is configured; clear it before polling")
        except Exception as exc:
            print_fn(f"❌ {name} check failed: {_safe_validation_error(exc)}")
            failures += 1
        else:
            print_fn(f"✅ {name} check passed")
    return failures


def _safe_validation_error(exc: Exception) -> str:
    if isinstance(exc, RuntimeError) and str(exc):
        return str(exc)
    return type(exc).__name__


def _contains_exact_word(output: str, value: str) -> bool:
    return re.search(
        rf"(?m)(^|[^A-Za-z0-9_-]){re.escape(value)}($|[^A-Za-z0-9_-])", output
    ) is not None


def _gateway_status_running(output: str) -> bool:
    lowered = output.lower()
    if any(
        marker in lowered
        for marker in ("not running", "not active", "stopped", "inactive", "failed")
    ):
        return False
    return re.search(r"\b(running|active)\b", output, re.IGNORECASE) is not None


def _validate_hermes(
    print_fn: PrintFn,
    command_runner: CommandRunner,
    coordinator_profile: str,
    incident_assignee: str,
    board: str,
) -> int:
    failures = 0
    result = command_runner(["hermes", "--version"], 30)
    if result.returncode != 0:
        print_fn("❌ Hermes CLI not found in PATH. Install it, then run `hermes doctor`.")
        return 1
    print_fn("✅ Hermes CLI found")

    for name, command in [
        ("Hermes profile list", ["hermes", "profile", "list"]),
        (
            "Hermes Kanban board list",
            ["hermes", "-p", coordinator_profile, "kanban", "boards", "list"],
        ),
        ("Hermes gateway status", ["hermes", "-p", coordinator_profile, "gateway", "status"]),
    ]:
        result = command_runner(command, 45)
        if result.returncode != 0:
            print_fn(f"❌ {name} failed. Command: {' '.join(command)}")
            failures += 1
            continue
        output = result.stdout + result.stderr
        if name == "Hermes profile list":
            missing = [
                profile
                for profile in (coordinator_profile, incident_assignee)
                if not _contains_exact_word(output, profile)
            ]
            if missing:
                print_fn(f"⚠️ Hermes profile(s) not found in output: {', '.join(missing)}")
                failures += len(missing)
        elif name == "Hermes Kanban board list" and not _contains_exact_word(output, board):
            print_fn(f"⚠️ Kanban board not found in output: {board}")
            failures += 1
        elif name == "Hermes gateway status" and not _gateway_status_running(output):
            print_fn("⚠️ Hermes gateway does not appear to be running")
            failures += 1
        else:
            print_fn(f"✅ {name} passed")
    return failures


def _run_systemd_install(print_fn: PrintFn, command_runner: CommandRunner, root: Path) -> int:
    script = root / "scripts" / "systemd-install.sh"
    if not script.exists():
        print_fn(f"❌ Missing systemd installer: {script}")
        return 1
    result = command_runner([str(script)], 60)
    if result.returncode != 0:
        print_fn("❌ systemd install failed; run scripts/systemd-install.sh manually for details.")
        return 1
    print_fn("✅ systemd unit files rendered; enable/start after dry-run verification")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="agent-alert-monitor-setup")
    parser.add_argument(
        "--root", default=".", help="Repository/runtime root to write config.yaml/.env"
    )
    parser.add_argument("--skip-live-checks", action="store_true")
    parser.add_argument("--force", action="store_true", help="Overwrite existing config.yaml/.env")
    args = parser.parse_args(argv)
    result = run_setup_wizard(
        root=Path(args.root), validate_live=not args.skip_live_checks, force=args.force
    )
    return 0 if result.checks_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
