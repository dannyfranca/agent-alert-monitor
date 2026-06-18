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


def _base_inputs(*, aws: bool = False, aws_dir: str = "") -> list[str]:
    values = [
        "",  # state dir
        "1",  # project count
        "Sample API",
        "sample-api",
        "",  # queue env default
        "https://sqs.sa-east-1.amazonaws.com/123/sample-api-alerts",
        "",  # dlq url env default
        "https://sqs.sa-east-1.amazonaws.com/123/sample-api-alerts-dlq",
        "",  # dlq arn env default
        "arn:aws:sqs:sa-east-1:123:sample-api-alerts-dlq",
        "",  # region default
        "",  # envelope default
        "",  # telegram status token env default
        "-100111",
        "alert-coordinator",
        "sample-api-incidents",
        "debugger",
        "",  # tenant default
        "",  # default priority
        "",  # critical priority
        "",  # message prefix
        "y" if aws else "n",
    ]
    if aws:
        values.extend(
            [
                aws_dir,
                "alert-monitor-readonly",
                "sa-east-1",
                "AKIAREADONLY",
            ]
        )
    values.append("n")  # install systemd
    return values


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
                    return {"ok": True, "result": {"id": 123456, "username": "sample_alert_bot"}}
                if url.endswith("/getChatMember"):
                    return {
                        "ok": True,
                        "result": {"status": "administrator", "can_post_messages": True},
                    }
                return {"ok": True, "result": {"id": -100111, "title": "Sample alerts"}}

        return Response()

    result = run_setup_wizard(
        root=tmp_path,
        input_fn=_input_from(_base_inputs(), prompts),
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
    project = config["projects"][0]
    assert project["sources"][0]["type"] == "aws_sqs"
    assert project["sources"][0]["queue_url_env"] == "SAMPLE_API_AGENT_ALERT_QUEUE_URL"
    assert project["sources"][0]["dlq_queue_url_env"] == "SAMPLE_API_AGENT_ALERT_DLQ_URL"
    assert project["sources"][0]["dlq_queue_arn_env"] == "SAMPLE_API_AGENT_ALERT_DLQ_ARN"
    assert project["sinks"][0]["bot_token_env"] == "ALERT_MONITOR_SAMPLE_API_TELEGRAM_BOT_TOKEN"
    assert project["sinks"][0]["chat_id"] == "-100111"
    assert project["hermes"]["kanban_board"] == "sample-api-incidents"
    assert project["kanban"]["incident_assignee"] == "debugger"

    env_text = result.env_path.read_text(encoding="utf-8")
    assert f"ALERT_MONITOR_STATE_DIR='{tmp_path / 'state'}'" in env_text
    queue_line = (
        "SAMPLE_API_AGENT_ALERT_QUEUE_URL="
        "'https://sqs.sa-east-1.amazonaws.com/123/sample-api-alerts'"
    )
    assert queue_line in env_text
    assert "ALERT_MONITOR_SQS_SOURCE='sample-api-prod-alerts'" in env_text
    assert (
        "SAMPLE_API_AGENT_ALERT_DLQ_URL="
        "'https://sqs.sa-east-1.amazonaws.com/123/sample-api-alerts-dlq'"
    ) in env_text
    assert (
        "SAMPLE_API_AGENT_ALERT_DLQ_ARN="
        "'arn:aws:sqs:sa-east-1:123:sample-api-alerts-dlq'"
    ) in env_text
    assert "ALERT_MONITOR_SAMPLE_API_TELEGRAM_BOT_TOKEN='123456:telegram-token'" in env_text

    rendered = "\n".join([*prompts, *output])
    assert "Existing dedicated SQS queue" in rendered
    assert "@BotFather" in rendered
    assert "status bot" in rendered
    assert "hermes profile create" in rendered
    assert "hermes -p alert-coordinator kanban boards create sample-api-incidents" in rendered
    assert "sqs-ingest --source sample-api-prod-alerts --dry-run" in rendered
    for removed in [
        "getUpdates",
        "deleteWebhook",
        "drop_pending_updates",
        "offset_path",
        "Telegram fallback",
        "listener bot",
    ]:
        assert removed not in rendered
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
                "https://sqs.sa-east-1.amazonaws.com/123/sample-api-alerts",
                "",
                "https://sqs.sa-east-1.amazonaws.com/123/sample-api-alerts-dlq",
                "",
                "arn:aws:sqs:sa-east-1:123:sample-api-alerts-dlq",
                "",
                "",
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
                "https://sqs.sa-east-1.amazonaws.com/123/worker-alerts",
                "",
                "https://sqs.sa-east-1.amazonaws.com/123/worker-alerts-dlq",
                "",
                "arn:aws:sqs:sa-east-1:123:worker-alerts-dlq",
                "",
                "",
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


def test_setup_wizard_collects_writes_and_validates_aws_readonly_credentials(
    tmp_path: Path,
) -> None:
    commands: list[Sequence[str]] = []
    output: list[str] = []
    aws_dir = tmp_path / "aws"

    def runner(command: Sequence[str], timeout: int = 30) -> CommandResult:
        commands.append(command)
        if command[:2] == ["hermes", "profile"]:
            return CommandResult(0, "alert-coordinator\ndebugger\n")
        if command[:4] == ["hermes", "-p", "alert-coordinator", "kanban"]:
            return CommandResult(0, "sample-api-incidents\n")
        if command[:4] == ["hermes", "-p", "alert-coordinator", "gateway"]:
            return CommandResult(0, "gateway running\n")
        if command[0] == "aws":
            return CommandResult(0, "{}\n")
        return CommandResult(0, "ok\n")

    def secret_fn(prompt: str) -> str:
        if "AWS secret access key" in prompt:
            return "aws-secret"
        return "123456:telegram-token"

    def telegram_get(url: str, params: dict[str, str | int] | None = None, timeout: int = 15):
        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                if url.endswith("/getMe"):
                    return {"ok": True, "result": {"id": 123456, "username": "sample_alert_bot"}}
                if url.endswith("/getChatMember"):
                    return {
                        "ok": True,
                        "result": {"status": "administrator", "can_post_messages": True},
                    }
                return {"ok": True, "result": {"id": -100111, "title": "Sample alerts"}}

        return Response()

    result = run_setup_wizard(
        root=tmp_path,
        input_fn=_input_from(_base_inputs(aws=True, aws_dir=str(aws_dir)), []),
        secret_fn=secret_fn,
        print_fn=output.append,
        command_runner=runner,
        telegram_get=telegram_get,
        validate_live=True,
        force=True,
    )

    assert result.checks_failed == 0
    env_text = result.env_path.read_text(encoding="utf-8")
    assert "AWS_PROFILE='alert-monitor-readonly'" in env_text
    assert "AWS_REGION='sa-east-1'" in env_text
    assert f"AWS_SHARED_CREDENTIALS_FILE='{aws_dir / 'credentials'}'" in env_text
    assert f"AWS_CONFIG_FILE='{aws_dir / 'config'}'" in env_text

    credentials_text = (aws_dir / "credentials").read_text(encoding="utf-8")
    assert "[alert-monitor-readonly]" in credentials_text
    assert "aws_access_key_id = AKIAREADONLY" in credentials_text
    assert "aws_secret_access_key = aws-secret" in credentials_text
    assert stat.S_IMODE((aws_dir / "credentials").stat().st_mode) == 0o600
    assert stat.S_IMODE((aws_dir / "config").stat().st_mode) == 0o600

    aws_commands = [command for command in commands if command and command[0] == "aws"]
    assert [
        "aws",
        "sts",
        "get-caller-identity",
        "--profile",
        "alert-monitor-readonly",
        "--region",
        "sa-east-1",
    ] in aws_commands
    assert any(command[:3] == ["aws", "cloudwatch", "describe-alarms"] for command in aws_commands)
    assert any(command[:3] == ["aws", "logs", "describe-log-groups"] for command in aws_commands)
    rendered = "\n".join(output)
    assert "CloudWatch" in rendered
    assert "dedicated IAM user access key" in rendered
    assert "Create access key" in rendered
    assert "AWS caller identity check passed" in rendered
