from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_alert_monitor.config import (
    AgentConfig,
    HermesConfig,
    KanbanConfig,
    RuntimeConfig,
    TelegramConfig,
    WatchdogConfig,
)
from agent_alert_monitor.coordinator import AlertCoordinator
from agent_alert_monitor.kanban import DryRunKanbanClient, KanbanCardRequest
from agent_alert_monitor.ledger import AlertLedger


class RecordingKanbanClient:
    def __init__(self) -> None:
        self.created_cards: list[KanbanCardRequest] = []

    def create_incident(self, request: KanbanCardRequest) -> str:
        self.created_cards.append(request)
        return f"t_{len(self.created_cards):08d}"

    def comment(self, task_id: str, body: str) -> None:
        raise AssertionError("comments are not used by these tests")


class FakeTelegramResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def telegram_update(update_id: int, message_id: int, text: str) -> dict[str, Any]:
    return {
        "ok": True,
        "result": [
            {
                "update_id": update_id,
                "channel_post": {
                    "message_id": message_id,
                    "chat": {"id": -100123},
                    "text": text,
                },
            }
        ],
    }


def make_config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        telegram=TelegramConfig(
            bot_token="test-token",
            alert_chat_id="-100123",
            offset_path=tmp_path / "telegram-offset.json",
        ),
        hermes=HermesConfig(coordinator_profile="alert-coordinator"),
        kanban=KanbanConfig(
            incident_assignee="debugger", default_priority=1000, critical_priority=2000
        ),
        runtime=RuntimeConfig(state_dir=tmp_path, ledger_path=tmp_path / "ledger.sqlite"),
        watchdog=WatchdogConfig(),
    )


def test_dry_run_synthetic_alert_plans_kanban_without_side_effects(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    kanban = DryRunKanbanClient()
    coordinator = AlertCoordinator(make_config(tmp_path), ledger=ledger, kanban_client=kanban)

    result = coordinator.handle_alert(
        platform="telegram",
        chat_id="-100123",
        message_id="77",
        raw_text="CRITICAL ALARM: Service5xx service=api region=us-east-1",
        dry_run=True,
    )

    assert result.action == "would_create_incident"
    assert result.incident_task_id is not None
    assert result.incident_task_id.startswith("dryrun-")
    assert kanban.created_cards == []
    assert ledger.count_messages() == 0
    assert result.planned_card is not None
    assert result.planned_card["assignee"] == "debugger"
    assert result.planned_card["priority"] == 2000
    assert "Status: investigating" in result.channel_message


def test_correlated_alert_updates_existing_incident_instead_of_new_card(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    kanban = RecordingKanbanClient()
    coordinator = AlertCoordinator(make_config(tmp_path), ledger=ledger, kanban_client=kanban)

    first = coordinator.handle_alert(
        "telegram", "-100123", "1", "ALARM: Service5xx service=api", dry_run=False
    )
    second = coordinator.handle_alert(
        "telegram", "-100123", "2", "ALARM: Service5xx service=api value=99", dry_run=False
    )

    assert first.action == "created_incident"
    assert second.action == "correlated"
    assert second.incident_task_id == first.incident_task_id
    assert len(kanban.created_cards) == 1
    assert "Status: correlated with existing incident" in second.channel_message


def test_same_alarm_in_different_projects_creates_separate_incidents(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    kanban = RecordingKanbanClient()
    alpha = make_config(tmp_path)
    alpha = AgentConfig(
        telegram=alpha.telegram,
        hermes=alpha.hermes,
        kanban=KanbanConfig(
            incident_assignee="alpha-debugger", default_priority=1000, critical_priority=2000
        ),
        runtime=alpha.runtime,
        watchdog=alpha.watchdog,
        project_slug="alpha-api",
        project_display_name="Alpha API",
    )
    beta = AgentConfig(
        telegram=alpha.telegram,
        hermes=alpha.hermes,
        kanban=KanbanConfig(
            incident_assignee="beta-debugger", default_priority=1000, critical_priority=2000
        ),
        runtime=alpha.runtime,
        watchdog=alpha.watchdog,
        project_slug="beta-worker",
        project_display_name="Beta Worker",
    )

    alpha_result = AlertCoordinator(alpha, ledger=ledger, kanban_client=kanban).handle_alert(
        "telegram", "-100123", "1", "ALARM: Service5xx service=api", dry_run=False
    )
    beta_result = AlertCoordinator(beta, ledger=ledger, kanban_client=kanban).handle_alert(
        "telegram", "-100123", "2", "ALARM: Service5xx service=api", dry_run=False
    )

    assert alpha_result.action == "created_incident"
    assert beta_result.action == "created_incident"
    assert alpha_result.incident_task_id != beta_result.incident_task_id
    assert len(kanban.created_cards) == 2
    assert kanban.created_cards[0].assignee == "alpha-debugger"
    assert kanban.created_cards[1].assignee == "beta-debugger"


def test_duplicate_without_incident_retries_kanban_create(tmp_path: Path) -> None:
    ledger = AlertLedger(tmp_path / "ledger.sqlite")
    ledger.ingest_message(
        platform="telegram",
        chat_id="-100123",
        message_id="1",
        raw_text="ALARM: Service5xx service=api",
    )
    kanban = RecordingKanbanClient()
    coordinator = AlertCoordinator(make_config(tmp_path), ledger=ledger, kanban_client=kanban)

    result = coordinator.handle_alert(
        "telegram", "-100123", "1", "ALARM: Service5xx service=api", dry_run=False
    )

    assert result.action == "created_incident"
    assert result.duplicate is True
    assert len(kanban.created_cards) == 1
