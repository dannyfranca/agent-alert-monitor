from __future__ import annotations

from pathlib import Path

from agent_alert_monitor.config import AwsSqsSourceConfig, load_config


def _sqs_project_yaml(
    *,
    slug: str = "sample-api",
    display_name: str = "Sample API",
    queue_env: str = "SAMPLE_QUEUE_URL",
    chat_id: str = "-100123",
    coordinator: str = "alert-coordinator",
    assignee: str = "debugger",
) -> str:
    return f"""
  - slug: {slug}
    display_name: {display_name}
    sources:
      - name: {slug}-prod-alerts
        type: aws_sqs
        queue_url_env: {queue_env}
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    sinks:
      - name: {slug}-telegram-status
        type: telegram
        bot_token_env: ALERT_MONITOR_{slug.replace('-', '_').upper()}_TELEGRAM_BOT_TOKEN
        chat_id: "{chat_id}"
    hermes:
      coordinator_profile: {coordinator}
      kanban_board: {slug}-incidents
      channel_target: telegram:{chat_id}
    kanban:
      tenant: {slug}
      incident_assignee: {assignee}
      default_priority: 1000
      critical_priority: 2000
"""


def test_load_config_expands_env_and_applies_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: ${{ALERT_MONITOR_STATE_DIR}}
projects:
{_sqs_project_yaml(queue_env="ALERT_MONITOR_SAMPLE_QUEUE_URL")}
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        env={
            "ALERT_MONITOR_SAMPLE_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/sample",
            "ALERT_MONITOR_STATE_DIR": str(tmp_path / "state"),
        },
    )

    assert cfg.telegram.bot_token == ""
    assert cfg.telegram.alert_chat_id == "-100123"
    assert cfg.kanban.incident_assignee == "debugger"
    assert cfg.kanban.default_priority == 1000
    assert cfg.kanban.critical_priority == 2000
    assert cfg.watchdog.ack_sla_seconds == 120
    assert cfg.runtime.ledger_path == tmp_path / "state" / "ledger.sqlite"


def test_offline_config_can_load_without_telegram_token(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: ./state
projects:
{_sqs_project_yaml(queue_env="ALERT_MONITOR_SAMPLE_QUEUE_URL")}
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        env={"ALERT_MONITOR_SAMPLE_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/sample"},
    )

    assert cfg.telegram.bot_token == ""


def test_load_config_supports_multiple_projects_and_project_selection(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    alpha_project = _sqs_project_yaml(
        slug="alpha-api",
        display_name="Alpha API",
        queue_env="ALPHA_QUEUE_URL",
        chat_id="-100111",
        coordinator="alpha-coordinator",
        assignee="alpha-debugger",
    )
    beta_project = _sqs_project_yaml(
        slug="beta-worker",
        display_name="Beta Worker",
        queue_env="BETA_QUEUE_URL",
        chat_id="-100222",
        coordinator="beta-coordinator",
        assignee="beta-debugger",
    )
    config_file.write_text(
        f"""
runtime:
  state_dir: ./state
projects:
{alpha_project}
{beta_project}
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        project_slug="beta-worker",
        env={"BETA_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/beta"},
    )

    assert [project.slug for project in cfg.projects] == ["alpha-api", "beta-worker"]
    assert cfg.project.slug == "beta-worker"
    assert cfg.project.display_name == "Beta Worker"
    assert cfg.telegram.alert_chat_id == "-100222"
    assert cfg.hermes.coordinator_profile == "beta-coordinator"
    assert cfg.kanban.tenant == "beta-worker"
    assert cfg.kanban.incident_assignee == "beta-debugger"
    assert cfg.runtime.ledger_path == tmp_path / "state" / "ledger.sqlite"


def test_selected_project_does_not_require_unselected_project_env(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    alpha_project = _sqs_project_yaml(
        slug="alpha-api",
        queue_env="ALPHA_QUEUE_URL",
        chat_id="-100111",
        coordinator="alpha-coordinator",
        assignee="alpha-debugger",
    )
    config_file.write_text(
        f"""
runtime:
  state_dir: ./state
projects:
{alpha_project}
  - slug: beta-worker
    sources:
      - name: beta-worker-alerts
        type: aws_sqs
        queue_url_env: BETA_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
        max_messages: "${{ALERT_MONITOR_BETA_MAX_MESSAGES}}"
    sinks:
      - name: beta-telegram
        type: telegram
        chat_id: "${{ALERT_MONITOR_BETA_CHAT_ID}}"
    hermes:
      coordinator_profile: beta-coordinator
    kanban:
      incident_assignee: beta-debugger
      default_priority: "${{ALERT_MONITOR_BETA_DEFAULT_PRIORITY}}"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        project_slug="alpha-api",
        env={"ALPHA_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/alpha"},
    )

    assert cfg.project.slug == "alpha-api"
    source = cfg.project.sources[0]
    assert isinstance(source, AwsSqsSourceConfig)
    assert source.queue_url.endswith("/alpha")


def test_explicit_project_does_not_require_env_driven_default_project(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    sample_project = _sqs_project_yaml(
        slug="sample-api",
        queue_env="SAMPLE_QUEUE_URL",
        chat_id="-100111",
        coordinator="sample-coordinator",
        assignee="sample-debugger",
    )
    worker_project = _sqs_project_yaml(
        slug="worker-queue",
        queue_env="WORKER_QUEUE_URL",
        chat_id="-100222",
        coordinator="worker-coordinator",
        assignee="worker-debugger",
    )
    config_file.write_text(
        f"""
default_project: ${{DEFAULT_PROJECT}}
runtime:
  state_dir: ./state
projects:
{sample_project}
{worker_project}
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        project_slug="worker-queue",
        env={"WORKER_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/worker"},
    )

    assert cfg.project.slug == "worker-queue"
    assert cfg.kanban.incident_assignee == "worker-debugger"


def test_default_project_env_placeholder_is_required_for_default_selection(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    sample_project = _sqs_project_yaml(
        slug="sample-api",
        queue_env="SAMPLE_QUEUE_URL",
        chat_id="-100111",
        coordinator="sample-coordinator",
        assignee="sample-debugger",
    )
    config_file.write_text(
        f"""
default_project: ${{DEFAULT_PROJECT}}
runtime:
  state_dir: ${{ALERT_MONITOR_STATE_DIR}}
projects:
{sample_project}
""".strip(),
        encoding="utf-8",
    )

    try:
        load_config(
            config_file,
            project_slug=None,
            env={"ALERT_MONITOR_STATE_DIR": str(tmp_path / "state")},
        )
    except ValueError as exc:
        assert "DEFAULT_PROJECT" in str(exc)
    else:
        raise AssertionError("expected missing default project environment error")


def test_unknown_project_selection_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    alpha_project = _sqs_project_yaml(
        slug="alpha-api",
        queue_env="ALPHA_QUEUE_URL",
        chat_id="-100111",
        coordinator="alpha-coordinator",
        assignee="alpha-debugger",
    )
    config_file.write_text(
        f"""
runtime:
  state_dir: ./state
projects:
{alpha_project}
""".strip(),
        encoding="utf-8",
    )

    try:
        load_config(config_file, project_slug="missing", env={})
    except ValueError as exc:
        assert "unknown project slug" in str(exc)
        assert "alpha-api" in str(exc)
    else:
        raise AssertionError("expected unknown project selection error")


def test_missing_env_placeholder_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"""
runtime:
  state_dir: ${{ALERT_MONITOR_STATE_DIR}}
projects:
{_sqs_project_yaml(queue_env="ALERT_MONITOR_SAMPLE_QUEUE_URL")}
""".strip(),
        encoding="utf-8",
    )

    try:
        load_config(
            config_file,
            env={"ALERT_MONITOR_SAMPLE_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/sample"},
        )
    except ValueError as exc:
        assert "ALERT_MONITOR_STATE_DIR" in str(exc)
    else:
        raise AssertionError("expected missing env placeholder error")
