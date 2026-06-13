from __future__ import annotations

from pathlib import Path


def test_systemd_units_persist_hermes_cli_path_for_live_mode() -> None:
    for unit_path in [
        Path("systemd/agent-alert-monitor-ingest.service"),
        Path("systemd/agent-alert-monitor-watchdog.service"),
    ]:
        text = unit_path.read_text(encoding="utf-8")
        assert "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin" in text


def test_live_mode_docs_include_systemd_hermes_path_smoke_test() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")

    assert (
        "systemctl --user show agent-alert-monitor-ingest.service --property=Environment"
        in readme
    )
    assert "hermes" in readme.split("## systemd user services", maxsplit=1)[1]
    assert "%h/.local/bin" in env_example


def test_operations_docs_require_backlog_drop_or_offset_priming_before_live_listen() -> None:
    text = Path("docs/operations.md").read_text(encoding="utf-8")
    non_dry_section = text.split("## Non-dry intake", maxsplit=1)[1]
    non_dry_section = non_dry_section.split("## Watchdog", maxsplit=1)[0]

    assert "drop_pending_updates" in non_dry_section
    assert "deleteWebhook" in non_dry_section
    assert "prime" in non_dry_section.lower()
    assert "offset" in non_dry_section.lower()
    paragraphs = [paragraph.lower() for paragraph in non_dry_section.split("\n\n")]
    assert any(
        "listen" in paragraph
        and "pending updates" in paragraph
        and "dropped" in paragraph
        and "offset" in paragraph
        and "primed" in paragraph
        for paragraph in paragraphs
    )
