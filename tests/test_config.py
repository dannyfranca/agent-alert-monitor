from __future__ import annotations

from pathlib import Path

from agent_alert_monitor.config import load_config


def test_load_config_expands_env_and_applies_defaults(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
telegram:
  bot_token_env: ALERT_MONITOR_TEST_BOT_TOKEN
  alert_chat_id: "-100123"
  poll_interval_seconds: 2
hermes:
  coordinator_profile: alert-coordinator
  kanban_board: /tmp/kanban.db
kanban:
  incident_assignee: debugger
  default_priority: 1000
  critical_priority: 2000
runtime:
  state_dir: ${ALERT_MONITOR_STATE_DIR}
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        env={
            "ALERT_MONITOR_TEST_BOT_TOKEN": "token-from-env",
            "ALERT_MONITOR_STATE_DIR": str(tmp_path / "state"),
        },
    )

    assert cfg.telegram.bot_token == "token-from-env"
    assert cfg.telegram.alert_chat_id == "-100123"
    assert cfg.telegram.poll_interval_seconds == 2
    assert cfg.kanban.incident_assignee == "debugger"
    assert cfg.kanban.default_priority == 1000
    assert cfg.kanban.critical_priority == 2000
    assert cfg.watchdog.ack_sla_seconds == 120
    assert cfg.runtime.ledger_path == tmp_path / "state" / "ledger.sqlite"


def test_offline_config_can_load_without_telegram_token(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
telegram:
  bot_token_env: ALERT_MONITOR_TELEGRAM_BOT_TOKEN
  alert_chat_id: "-100123"
hermes:
  coordinator_profile: alert-coordinator
kanban:
  incident_assignee: debugger
runtime:
  state_dir: ./state
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(config_file, env={})

    assert cfg.telegram.bot_token == ""


def test_load_config_supports_multiple_projects_and_project_selection(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: alpha-api
    display_name: Alpha API
    telegram:
      bot_token_env: ALERT_MONITOR_ALPHA_BOT_TOKEN
      alert_chat_id: "-100111"
      offset_path: "./state/alpha-offset.json"
    hermes:
      coordinator_profile: alpha-coordinator
      kanban_board: alpha-incidents
      channel_target: telegram:-100111
    kanban:
      tenant: alpha
      incident_assignee: alpha-debugger
      default_priority: 900
      critical_priority: 1900
    messages:
      prefix: Alpha alert monitor
  - slug: beta-worker
    display_name: Beta Worker
    telegram:
      bot_token_env: ALERT_MONITOR_BETA_BOT_TOKEN
      alert_chat_id: "-100222"
    hermes:
      coordinator_profile: beta-coordinator
      kanban_board: beta-incidents
    kanban:
      tenant: beta
      incident_assignee: beta-debugger
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        project_slug="beta-worker",
        env={
            "ALERT_MONITOR_ALPHA_BOT_TOKEN": "alpha-token",
            "ALERT_MONITOR_BETA_BOT_TOKEN": "beta-token",
        },
    )

    assert [project.slug for project in cfg.projects] == ["alpha-api", "beta-worker"]
    assert cfg.project.slug == "beta-worker"
    assert cfg.project.display_name == "Beta Worker"
    assert cfg.telegram.bot_token == "beta-token"
    assert cfg.telegram.alert_chat_id == "-100222"
    assert cfg.hermes.coordinator_profile == "beta-coordinator"
    assert cfg.kanban.tenant == "beta"
    assert cfg.kanban.incident_assignee == "beta-debugger"
    assert cfg.runtime.ledger_path == tmp_path / "state" / "ledger.sqlite"


def test_selected_project_does_not_require_unselected_project_env(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: alpha-api
    telegram:
      bot_token_env: ALERT_MONITOR_ALPHA_BOT_TOKEN
      alert_chat_id: "-100111"
    hermes:
      coordinator_profile: alpha-coordinator
    kanban:
      incident_assignee: alpha-debugger
  - slug: beta-worker
    telegram:
      bot_token_env: ALERT_MONITOR_BETA_BOT_TOKEN
      alert_chat_id: "${ALERT_MONITOR_BETA_CHAT_ID}"
      poll_interval_seconds: "${ALERT_MONITOR_BETA_POLL_SECONDS}"
    hermes:
      coordinator_profile: beta-coordinator
    kanban:
      incident_assignee: beta-debugger
      default_priority: "${ALERT_MONITOR_BETA_DEFAULT_PRIORITY}"
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        project_slug="alpha-api",
        env={"ALERT_MONITOR_ALPHA_BOT_TOKEN": "alpha-token"},
    )

    assert cfg.project.slug == "alpha-api"
    assert cfg.telegram.bot_token == "alpha-token"


def test_explicit_project_does_not_require_env_driven_default_project(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
default_project: ${DEFAULT_PROJECT}
runtime:
  state_dir: ./state
projects:
  - slug: sample-api
    telegram:
      alert_chat_id: "-100111"
    hermes:
      coordinator_profile: sample-coordinator
    kanban:
      incident_assignee: sample-debugger
  - slug: worker-queue
    telegram:
      alert_chat_id: "-100222"
    hermes:
      coordinator_profile: worker-coordinator
    kanban:
      incident_assignee: worker-debugger
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(config_file, project_slug="worker-queue", env={})

    assert cfg.project.slug == "worker-queue"
    assert cfg.kanban.incident_assignee == "worker-debugger"


def test_default_project_env_placeholder_is_required_for_default_selection(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
default_project: ${DEFAULT_PROJECT}
runtime:
  state_dir: ${ALERT_MONITOR_STATE_DIR}
projects:
  - slug: sample-api
    telegram:
      alert_chat_id: "-100111"
    hermes:
      coordinator_profile: sample-coordinator
    kanban:
      incident_assignee: sample-debugger
  - slug: worker-queue
    telegram:
      alert_chat_id: "-100222"
    hermes:
      coordinator_profile: worker-coordinator
    kanban:
      incident_assignee: worker-debugger
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
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: alpha-api
    display_name: Alpha API
    telegram:
      alert_chat_id: "-100111"
    hermes:
      coordinator_profile: alpha-coordinator
    kanban:
      incident_assignee: alpha-debugger
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
        """
telegram:
  bot_token_env: ALERT_MONITOR_TEST_BOT_TOKEN
  alert_chat_id: "-100123"
hermes:
  coordinator_profile: alert-coordinator
kanban:
  incident_assignee: debugger
runtime:
  state_dir: ${ALERT_MONITOR_STATE_DIR}
""".strip(),
        encoding="utf-8",
    )

    try:
        load_config(config_file, env={"ALERT_MONITOR_TEST_BOT_TOKEN": "token"})
    except ValueError as exc:
        assert "ALERT_MONITOR_STATE_DIR" in str(exc)
    else:
        raise AssertionError("expected missing env placeholder error")
