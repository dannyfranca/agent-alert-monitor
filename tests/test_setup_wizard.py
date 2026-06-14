from __future__ import annotations

import stat
from collections.abc import Sequence
from pathlib import Path

import yaml

from agent_alert_monitor.setup_wizard import CommandResult, run_setup_wizard


def _input_from(values: list[str], prompts: list[str]):
    iterator = iter(values)

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        return next(iterator)

    return input_fn


def test_interactive_setup_writes_config_env_and_prints_data_instructions(tmp_path: Path) -> None:
    prompts: list[str] = []
    output: list[str] = []
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], timeout: int = 30) -> CommandResult:
        commands.append(command)
        if command[:2] == ["hermes", "profile"]:
            return CommandResult(0, "alert-coordinator\ndebugger\n")
        if command[:4] == ["hermes", "-p", "alert-coordinator", "kanban"]:
            return CommandResult(0, "sample-api-incidents\n")
        if command[:4] == ["hermes", "-p", "alert-coordinator", "gateway"]:
            return CommandResult(0, "gateway running\n")
        return CommandResult(0, "ok\n")

    def telegram_get(url: str, params: dict[str, str | int] | None = None, timeout: int = 15):
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                if url.endswith("/getMe"):
                    return {"ok": True, "result": {"username": "sample_alert_bot"}}
                return {"ok": True, "result": {"id": -100111, "title": "Sample alerts"}}

        return Response()

    result = run_setup_wizard(
        root=tmp_path,
        input_fn=_input_from(
            [
                "",  # state dir
                "1",  # project count
                "Sample API",
                "sample-api",
                "",  # token env default
                "-100111",
                "alert-coordinator",
                "sample-api-incidents",
                "debugger",
                "",  # tenant default
                "",  # default priority
                "",  # critical priority
                "",  # message prefix
                "n",  # optional AWS instructions
                "n",  # install systemd
            ],
            prompts,
        ),
        secret_fn=lambda prompt: "123456:telegram-token",
        print_fn=output.append,
        command_runner=runner,
        telegram_get=telegram_get,
        validate_live=True,
        force=True,
    )

    assert result.config_path == tmp_path / "config.yaml"
    assert result.env_path == tmp_path / ".env"
    assert result.checks_failed == 0
    assert (tmp_path / "state").is_dir()
    assert stat.S_IMODE(result.env_path.stat().st_mode) == 0o600

    config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))
    assert config["default_project"] == "sample-api"
    assert config["projects"][0]["telegram"]["bot_token_env"] == (
        "ALERT_MONITOR_SAMPLE_API_TELEGRAM_BOT_TOKEN"
    )
    assert config["projects"][0]["hermes"]["kanban_board"] == "sample-api-incidents"
    assert config["projects"][0]["kanban"]["incident_assignee"] == "debugger"

    env_text = result.env_path.read_text(encoding="utf-8")
    assert f"ALERT_MONITOR_STATE_DIR='{tmp_path / 'state'}'" in env_text
    assert "ALERT_MONITOR_SAMPLE_API_TELEGRAM_BOT_TOKEN='123456:telegram-token'" in env_text

    rendered = "\n".join([*prompts, *output])
    assert "@BotFather" in rendered
    assert "add it as an admin" in rendered
    assert "hermes profile create" in rendered
    assert "hermes -p alert-coordinator kanban boards create sample-api-incidents" in rendered
    assert ["hermes", "profile", "list"] in commands


def test_setup_wizard_refuses_to_overwrite_existing_files_without_force(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("existing: true\n", encoding="utf-8")

    result = run_setup_wizard(
        root=tmp_path,
        input_fn=lambda prompt: "",
        secret_fn=lambda prompt: "",
        print_fn=lambda text: None,
        validate_live=False,
        force=False,
    )

    assert result.checks_failed == 1
    assert "already exists" in result.messages[0]
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "existing: true\n"


def test_setup_wizard_rejects_zero_project_count(tmp_path: Path) -> None:
    output: list[str] = []

    result = run_setup_wizard(
        root=tmp_path,
        input_fn=_input_from(["", "0"], []),
        secret_fn=lambda prompt: "",
        print_fn=output.append,
        validate_live=False,
        force=True,
    )

    assert result.checks_failed == 1
    assert "at least one project" in result.messages[0]
    assert not (tmp_path / "config.yaml").exists()


def test_setup_wizard_repompts_duplicate_project_slugs(tmp_path: Path) -> None:
    result = run_setup_wizard(
        root=tmp_path,
        input_fn=_input_from(
            [
                "",
                "2",
                "Sample API",
                "sample-api",
                "",
                "-100111",
                "alert-coordinator",
                "sample-api-incidents",
                "debugger",
                "",
                "",
                "",
                "",
                "Worker Queue",
                "sample-api",
                "worker-queue",
                "",
                "-100222",
                "worker-coordinator",
                "worker-incidents",
                "worker-debugger",
                "",
                "",
                "",
                "",
                "n",
                "n",
            ],
            [],
        ),
        secret_fn=lambda prompt: "token",
        print_fn=lambda text: None,
        validate_live=False,
        force=True,
    )

    assert result.checks_failed == 0
    config = yaml.safe_load(result.config_path.read_text(encoding="utf-8"))
    assert [project["slug"] for project in config["projects"]] == [
        "sample-api",
        "worker-queue",
    ]
