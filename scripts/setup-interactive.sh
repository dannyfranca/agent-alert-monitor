#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -x .venv/bin/agent-alert-monitor ]; then
  printf 'Local .venv is missing. Running scripts/install.sh first...\n'
  ./scripts/install.sh
fi

exec .venv/bin/agent-alert-monitor setup --root "$ROOT" "$@"
