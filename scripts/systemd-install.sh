#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

python3 - "$ROOT" "$UNIT_DIR" <<'PY'
from __future__ import annotations

import pathlib
import sys

root = pathlib.Path(sys.argv[1])
unit_dir = pathlib.Path(sys.argv[2])


def systemd_quote_path(path: pathlib.Path) -> str:
    value = str(path)
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{value}"'

replacements = {
    "__ALERT_MONITOR_ROOT_SYSTEMD__": systemd_quote_path(root),
    "__ALERT_MONITOR_ENV_SYSTEMD__": systemd_quote_path(root / ".env"),
    "__ALERT_MONITOR_BIN_SYSTEMD__": systemd_quote_path(
        root / ".venv" / "bin" / "agent-alert-monitor"
    ),
    "__ALERT_MONITOR_CONFIG_SYSTEMD__": systemd_quote_path(root / "config.yaml"),
}

for unit in [
    "agent-alert-monitor-watchdog.service",
    "agent-alert-monitor-watchdog.timer",
    "agent-alert-monitor-sqs-readiness.service",
    "agent-alert-monitor-sqs-listen.service",
    "agent-alert-monitor-health.service",
    "agent-alert-monitor-health.timer",
]:
    rendered = (root / "systemd" / unit).read_text(encoding="utf-8")
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    (unit_dir / unit).write_text(rendered, encoding="utf-8")
PY

legacy_unit="$UNIT_DIR/agent-alert-monitor-ingest.service"
if systemctl --user list-unit-files --no-legend 'agent-alert-monitor-ingest.service' | grep -q '^agent-alert-monitor-ingest.service'; then
  systemctl --user disable --now agent-alert-monitor-ingest.service >/dev/null 2>&1 || true
fi
rm -f "$legacy_unit"

systemctl --user daemon-reload
printf 'Installed units under %s for repo %s.\n' "$UNIT_DIR" "$ROOT"
printf 'Next: systemctl --user enable --now agent-alert-monitor-sqs-readiness.service agent-alert-monitor-health.timer agent-alert-monitor-watchdog.timer\n'
