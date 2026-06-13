from __future__ import annotations

from pathlib import Path


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
