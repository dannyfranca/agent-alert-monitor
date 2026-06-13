from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_KANBAN_CREATE_STDIN_SCRIPT = r"""
from __future__ import annotations

import json
import os
import sys

payload = json.load(sys.stdin)
profile = payload.get("profile")
if profile:
    from hermes_cli.profiles import resolve_profile_env

    os.environ["HERMES_HOME"] = resolve_profile_env(profile)
    os.environ["HERMES_PROFILE"] = profile

from hermes_cli.env_loader import load_hermes_dotenv

load_hermes_dotenv()

board = payload.get("board")
if not isinstance(board, str) or not board.strip():
    print("ALERT_MONITOR_REQUIRED_KANBAN_BOARD", file=sys.stderr)
    raise SystemExit(66) from None

expanded_board = os.path.expandvars(os.path.expanduser(board.strip()))
os.environ.pop("HERMES_KANBAN_DB", None)
os.environ["HERMES_KANBAN_BOARD"] = expanded_board

from hermes_cli import kanban_db as kb

try:
    board_exists = kb.board_exists(expanded_board)
except ValueError:
    print("ALERT_MONITOR_INVALID_KANBAN_BOARD", file=sys.stderr)
    raise SystemExit(66) from None
if not board_exists:
    print("ALERT_MONITOR_MISSING_KANBAN_BOARD", file=sys.stderr)
    raise SystemExit(66) from None


def profile_author() -> str:
    return os.environ.get("HERMES_PROFILE") or os.environ.get("USER") or "user"


with kb.connect_closing() as conn:
    task_id = kb.create_task(
        conn,
        title=payload["title"],
        body=payload["body"],
        assignee=payload["assignee"],
        created_by=profile_author(),
        workspace_kind="scratch",
        workspace_path=None,
        branch_name=None,
        tenant=payload["tenant"],
        priority=int(payload["priority"]),
        parents=(),
        triage=False,
        idempotency_key=payload["idempotency_key"],
        max_runtime_seconds=None,
        skills=None,
        max_retries=None,
        goal_mode=False,
        goal_max_turns=None,
        initial_status="running",
    )
print(json.dumps({"task_id": task_id, "id": task_id}))
"""


@dataclass(frozen=True)
class KanbanCardRequest:
    title: str
    assignee: str
    body: str
    priority: int
    tenant: str
    idempotency_key: str


class KanbanClient(Protocol):
    def create_incident(self, request: KanbanCardRequest) -> str: ...

    def comment(self, task_id: str, body: str) -> None: ...


class DryRunKanbanClient:
    """No-side-effect client used by tests, demos, and synthetic dry-runs."""

    def __init__(self) -> None:
        self.created_cards: list[KanbanCardRequest] = []
        self.comments: list[tuple[str, str]] = []

    def create_incident(self, request: KanbanCardRequest) -> str:
        self.created_cards.append(request)
        return f"dryrun-{request.idempotency_key.rsplit(':', 1)[-1][:8]}"

    def comment(self, task_id: str, body: str) -> None:
        self.comments.append((task_id, body))


class HermesKanbanCliClient:
    """Small CLI adapter for normal, non-dry-run use outside an agent process.

    Agent workers should prefer the native kanban tools. This adapter exists so a
    local systemd poller can create/comment cards without exposing a public VM
    endpoint. Card bodies are sent to a short Hermes-side Python helper over
    stdin so raw alert metadata does not appear in subprocess argv.
    """

    def __init__(
        self,
        hermes_bin: str = "hermes",
        profile: str | None = None,
        board: str | None = None,
    ) -> None:
        self.hermes_bin = hermes_bin
        self.profile = profile
        self.board = board

    def _base_cmd(self) -> list[str]:
        cmd = [self.hermes_bin]
        if self.profile:
            cmd.extend(["--profile", self.profile])
        return cmd

    def _configured_board(self) -> str:
        if self.board is None or not self.board.strip():
            raise RuntimeError(
                "Hermes Kanban create failed: configured Kanban board is required; "
                "no card was created"
            ) from None
        return os.path.expandvars(os.path.expanduser(self.board.strip()))

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        board = self._configured_board()
        env.pop("HERMES_KANBAN_DB", None)
        env["HERMES_KANBAN_BOARD"] = board
        return env

    def create_incident(self, request: KanbanCardRequest) -> str:
        board = self._configured_board()
        payload = {
            "title": request.title,
            "assignee": request.assignee,
            "body": request.body,
            "priority": request.priority,
            "tenant": request.tenant,
            "idempotency_key": request.idempotency_key,
            "profile": self.profile,
            "board": board,
        }
        cmd = [self._hermes_python(), "-c", _KANBAN_CREATE_STDIN_SCRIPT]
        try:
            proc = subprocess.run(
                cmd,
                input=json.dumps(payload),
                check=True,
                text=True,
                capture_output=True,
                env=self._env(),
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            if "ALERT_MONITOR_REQUIRED_KANBAN_BOARD" in stderr:
                raise RuntimeError(
                    "Hermes Kanban create failed: configured Kanban board is required; "
                    "no card was created"
                ) from None
            if "ALERT_MONITOR_MISSING_KANBAN_BOARD" in stderr:
                raise RuntimeError(
                    "Hermes Kanban create failed: configured Kanban board does not exist; "
                    "no card was created"
                ) from None
            if "ALERT_MONITOR_INVALID_KANBAN_BOARD" in stderr:
                raise RuntimeError(
                    "Hermes Kanban create failed: configured Kanban board slug is invalid; "
                    "no card was created"
                ) from None
            raise RuntimeError(
                f"Hermes Kanban create failed with exit code {exc.returncode}; "
                "subprocess output omitted to avoid leaking raw alert metadata"
            ) from None
        payload_out = json.loads(proc.stdout)
        task_id = payload_out.get("task_id") or payload_out.get("id")
        if not task_id:
            raise RuntimeError("Hermes Kanban create succeeded but returned no task id")
        return str(task_id)

    def _hermes_python(self) -> str:
        hermes_path = Path(shutil.which(self.hermes_bin) or self.hermes_bin)
        try:
            first_line = hermes_path.read_text(encoding="utf-8").splitlines()[0]
        except (IndexError, OSError, UnicodeDecodeError):
            return str(hermes_path)
        if not first_line.startswith("#!"):
            return str(hermes_path)
        shebang = shlex.split(first_line[2:].strip())
        if not shebang:
            return str(hermes_path)
        if Path(shebang[0]).name == "env" and len(shebang) > 1:
            return shutil.which(shebang[1]) or shebang[1]
        return shebang[0]

    def comment(self, task_id: str, body: str) -> None:
        subprocess.run(
            [*self._base_cmd(), "kanban", "comment", task_id, body],
            check=True,
            text=True,
            capture_output=True,
            env=self._env(),
        )
