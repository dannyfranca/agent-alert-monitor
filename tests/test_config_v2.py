from __future__ import annotations

from pathlib import Path

from agent_alert_monitor.config import (
    AwsSqsSourceConfig,
    TelegramSinkConfig,
    TelegramSourceConfig,
    load_config,
)


def test_legacy_telegram_project_config_creates_compatible_source_and_sink(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: legacy-api
    display_name: Legacy API
    telegram:
      bot_token_env: ALERT_MONITOR_LEGACY_BOT_TOKEN
      alert_chat_id: "-100111"
      poll_interval_seconds: 7
    hermes:
      coordinator_profile: legacy-coordinator
      kanban_board: legacy-incidents
    kanban:
      incident_assignee: legacy-debugger
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(config_file, env={"ALERT_MONITOR_LEGACY_BOT_TOKEN": "legacy-token"})

    assert cfg.telegram.bot_token == "legacy-token"
    assert cfg.telegram.alert_chat_id == "-100111"
    assert cfg.telegram.poll_interval_seconds == 7
    assert len(cfg.project.sources) == 1
    assert isinstance(cfg.project.sources[0], TelegramSourceConfig)
    assert cfg.project.sources[0].type == "telegram"
    assert len(cfg.project.sinks) == 1
    assert isinstance(cfg.project.sinks[0], TelegramSinkConfig)
    assert cfg.project.sinks[0].chat_id == "-100111"


def test_v2_sources_and_sinks_config_validates_without_legacy_telegram_section(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ${ALERT_MONITOR_STATE_DIR}
default_project: ticketdovale
projects:
  - slug: ticketdovale
    display_name: TicketDoVale
    environment: prod
    sources:
      - name: ticketdovale-prod-alerts
        type: aws_sqs
        queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
        wait_time_seconds: 20
        max_messages: 10
        visibility_timeout_seconds: 300
        delete_policy: after_successful_side_effects
    sinks:
      - name: ticketdovale-telegram-status
        type: telegram
        bot_token_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN
        chat_id_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID
    hermes:
      coordinator_profile: alert-coordinator
      kanban_board: ticketdovale-incidents
      channel_target: telegram:${ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID}
    kanban:
      incident_assignee: debugger
      coder_assignee: coder
      reviewer_assignee: reviewer
      tenant: ticketdovale
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        env={
            "ALERT_MONITOR_STATE_DIR": str(tmp_path / "state"),
            "TICKETDOVALE_AGENT_ALERT_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/agent-alert-monitor-ticketdovale-prod",
            "ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN": "telegram-token",
            "ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID": "-100222",
        },
    )

    assert cfg.project.slug == "ticketdovale"
    assert cfg.project.environment == "prod"
    assert len(cfg.project.sources) == 1
    source = cfg.project.sources[0]
    assert isinstance(source, AwsSqsSourceConfig)
    assert source.name == "ticketdovale-prod-alerts"
    assert source.queue_url == "https://sqs.sa-east-1.amazonaws.com/123/agent-alert-monitor-ticketdovale-prod"
    assert source.queue_url_env == "TICKETDOVALE_AGENT_ALERT_QUEUE_URL"
    assert source.region == "sa-east-1"
    assert source.envelope == "aws_sns_cloudwatch_alarm"
    assert source.wait_time_seconds == 20
    assert source.max_messages == 10
    assert source.visibility_timeout_seconds == 300
    assert source.delete_policy == "after_successful_side_effects"

    assert len(cfg.project.sinks) == 1
    sink = cfg.project.sinks[0]
    assert isinstance(sink, TelegramSinkConfig)
    assert sink.chat_id == "-100222"
    assert sink.chat_id_env == "ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID"
    assert cfg.telegram.bot_token == "telegram-token"
    assert cfg.telegram.alert_chat_id == "-100222"
    assert cfg.project.telegram_source is None
    assert cfg.project.telegram_sink == sink
    assert cfg.hermes.channel_target == "telegram:-100222"


def test_explicit_telegram_sink_is_selected_for_status_when_legacy_telegram_exists(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: mixed
    telegram:
      bot_token_env: ALERT_MONITOR_LEGACY_BOT_TOKEN
      alert_chat_id: "-100111"
    sources:
      - name: mixed-alerts
        type: aws_sqs
        queue_url_env: MIXED_QUEUE_URL
        region: sa-east-1
        envelope: aws_sns_cloudwatch_alarm
    sinks:
      - name: mixed-telegram-status
        type: telegram
        bot_token_env: ALERT_MONITOR_STATUS_BOT_TOKEN
        chat_id: "-100222"
    hermes:
      coordinator_profile: mixed-coordinator
    kanban:
      incident_assignee: mixed-debugger
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(
        config_file,
        env={
            "MIXED_QUEUE_URL": "https://sqs.sa-east-1.amazonaws.com/123/mixed",
            "ALERT_MONITOR_LEGACY_BOT_TOKEN": "legacy-token",
            "ALERT_MONITOR_STATUS_BOT_TOKEN": "status-token",
        },
    )

    assert cfg.project.telegram_source is None
    assert cfg.telegram.bot_token == "status-token"
    assert cfg.telegram.alert_chat_id == "-100222"


def test_selected_project_does_not_require_unselected_v2_source_env(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: alpha
    sources:
      - name: alpha-alerts
        type: aws_sqs
        queue_url_env: ALPHA_QUEUE_URL
        region: us-east-1
        envelope: aws_eventbridge_cloudwatch_alarm
    sinks:
      - name: alpha-telegram
        type: telegram
        chat_id_env: ALPHA_CHAT_ID
    hermes:
      coordinator_profile: alpha-coordinator
    kanban:
      incident_assignee: alpha-debugger
  - slug: beta
    telegram:
      alert_chat_id: "-100222"
    hermes:
      coordinator_profile: beta-coordinator
    kanban:
      incident_assignee: beta-debugger
""".strip(),
        encoding="utf-8",
    )

    cfg = load_config(config_file, project_slug="beta", env={})

    assert cfg.project.slug == "beta"
    assert cfg.telegram.alert_chat_id == "-100222"


def test_project_without_legacy_telegram_or_v2_sources_is_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
runtime:
  state_dir: ./state
projects:
  - slug: broken
    sinks:
      - name: broken-telegram
        type: telegram
        chat_id: "-100333"
    hermes:
      coordinator_profile: broken-coordinator
    kanban:
      incident_assignee: broken-debugger
""".strip(),
        encoding="utf-8",
    )

    try:
        load_config(config_file, env={})
    except ValueError as exc:
        assert "requires telegram config or explicit sources" in str(exc)
    else:
        raise AssertionError("expected missing intake configuration error")
