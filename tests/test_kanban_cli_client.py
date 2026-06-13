from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from agent_alert_monitor.kanban import HermesKanbanCliClient, KanbanCardRequest


def card_request(body: str = "raw production alert text service=api") -> KanbanCardRequest:
    return KanbanCardRequest(
        title="Application alert: Service5xx",
        assignee="debugger",
        body=body,
        priority=2000,
        tenant="application",
        idempotency_key="alert-monitor:fingerprint:message-id",
    )


def test_create_incident_passes_sensitive_body_over_stdin_not_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    secret_body = "raw production alert text service=api account=private"

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"task_id": "t_12345678"}))

    monkeypatch.setattr(subprocess, "run", fake_run)

    client = HermesKanbanCliClient(
        hermes_bin="/usr/local/bin/hermes",
        profile="alert-coordinator",
        board="alerts",
    )
    task_id = client.create_incident(card_request(secret_body))

    assert task_id == "t_12345678"
    assert all(secret_body not in arg for arg in captured["cmd"])
    assert "--body" not in captured["cmd"]
    payload = json.loads(captured["input"])
    assert payload["body"] == secret_body


def test_create_incident_failure_does_not_echo_sensitive_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_body = "raw production alert text account=private"

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            2,
            cmd,
            output="",
            stderr=f"simulated failure while creating {secret_body}",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        client = HermesKanbanCliClient(hermes_bin="/usr/local/bin/hermes", board="alerts")
        client.create_incident(card_request(secret_body))

    assert secret_body not in str(excinfo.value)
    assert "Kanban create failed" in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


@pytest.mark.parametrize("board", [None, "", "   "])
def test_create_incident_requires_explicit_board_before_subprocess(
    board: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_body = "raw production alert text account=private"

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess should not run without an explicit board")

    monkeypatch.setattr(subprocess, "run", fake_run)

    client = HermesKanbanCliClient(hermes_bin="/usr/local/bin/hermes", board=board)
    with pytest.raises(RuntimeError) as excinfo:
        client.create_incident(card_request(secret_body))

    message = str(excinfo.value)
    assert "Kanban create failed" in message
    assert "configured Kanban board is required" in message
    assert "no card was created" in message
    assert secret_body not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True


def test_create_incident_missing_named_board_fails_before_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pkg = tmp_path / "fake_hermes" / "hermes_cli"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text("", encoding="utf-8")
    (fake_pkg / "env_loader.py").write_text(
        "def load_hermes_dotenv():\n    return None\n", encoding="utf-8"
    )
    (fake_pkg / "profiles.py").write_text(
        "def resolve_profile_env(profile):\n    return '/tmp/fake-hermes-home'\n",
        encoding="utf-8",
    )
    marker = tmp_path / "create-called"
    (fake_pkg / "kanban_db.py").write_text(
        textwrap.dedent(
            f"""
            from contextlib import contextmanager
            from pathlib import Path

            DEFAULT_BOARD = "default"

            def board_exists(board=None):
                return board in (None, "default")

            @contextmanager
            def connect_closing():
                yield object()

            def create_task(*args, **kwargs):
                Path({str(marker)!r}).write_text("called", encoding="utf-8")
                return "t_wrong_board"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "fake_hermes"))

    client = HermesKanbanCliClient(hermes_bin=sys.executable, board="missing-board")

    with pytest.raises(RuntimeError) as excinfo:
        client.create_incident(card_request("raw production alert text account=private"))

    message = str(excinfo.value)
    assert "Kanban create failed" in message
    assert "raw production alert text" not in message
    assert "ALERT_MONITOR_MISSING_KANBAN_BOARD" not in message
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True
    assert not marker.exists()


def test_create_incident_configured_board_clears_db_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pkg = tmp_path / "fake_hermes" / "hermes_cli"
    fake_pkg.mkdir(parents=True)
    (fake_pkg / "__init__.py").write_text("", encoding="utf-8")
    (fake_pkg / "env_loader.py").write_text(
        "def load_hermes_dotenv():\n    return None\n", encoding="utf-8"
    )
    (fake_pkg / "profiles.py").write_text(
        "def resolve_profile_env(profile):\n    return '/tmp/fake-hermes-home'\n",
        encoding="utf-8",
    )
    observed_db_override = tmp_path / "observed-db-override"
    observed_db_at_validation = tmp_path / "observed-db-at-validation"
    observed_board = tmp_path / "observed-board"
    (fake_pkg / "kanban_db.py").write_text(
        textwrap.dedent(
            f"""
            from contextlib import contextmanager
            from pathlib import Path
            import os

            DEFAULT_BOARD = "default"

            def board_exists(board=None):
                Path({str(observed_db_at_validation)!r}).write_text(
                    os.environ.get("HERMES_KANBAN_DB", ""), encoding="utf-8"
                )
                return board in (None, "default", "alerts")

            @contextmanager
            def connect_closing():
                yield object()

            def create_task(*args, **kwargs):
                Path({str(observed_db_override)!r}).write_text(
                    os.environ.get("HERMES_KANBAN_DB", ""), encoding="utf-8"
                )
                Path({str(observed_board)!r}).write_text(
                    os.environ.get("HERMES_KANBAN_BOARD", ""), encoding="utf-8"
                )
                return "t_alerts_board"
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "fake_hermes"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "default-kanban.db"))

    client = HermesKanbanCliClient(hermes_bin=sys.executable, board="alerts")

    assert client.create_incident(card_request()) == "t_alerts_board"
    assert observed_db_at_validation.read_text(encoding="utf-8") == ""
    assert observed_db_override.read_text(encoding="utf-8") == ""
    assert observed_board.read_text(encoding="utf-8") == "alerts"
