# Operations

## First run

```bash
./scripts/install.sh
install -m 600 .env.example .env
cp config.example.yaml config.yaml
# edit .env and config.yaml locally
source .venv/bin/activate
set -a; . ./.env; set +a
agent-alert-monitor --config config.yaml --project sample-api synthetic-alert --text 'CRITICAL ALARM: Service5xx service=api' --dry-run
agent-alert-monitor --config config.yaml --project worker-queue synthetic-alert --text 'ALARM: QueueDepth service=worker' --dry-run
```

## Non-dry intake

Start with dry-run polling until Telegram filtering and planned output look correct:

```bash
# Poll all configured projects once without Telegram/Kanban side effects.
agent-alert-monitor --config config.yaml ingest --dry-run

# Or poll one project only.
agent-alert-monitor --config config.yaml --project sample-api ingest --dry-run
```

Do not switch from dry-run polling directly to non-dry `listen`. Dry-run mode does not write the configured project offset files, so Telegram can still have old pending `getUpdates` items that would be replayed into real Kanban cards and alert-channel status messages.

Before enabling the non-dry `listen` service, do one of these backlog-safety steps for every configured project:

1. Preferred first live run: clear the polling backlog with Telegram `deleteWebhook(drop_pending_updates=true)` for that project's bot token and confirm `getWebhookInfo` has an empty webhook URL, as shown in the README Telegram setup.
2. If the backlog must be preserved for investigation: explicitly prime the configured offset. Page through `getUpdates` until no pending updates remain, inspect each page, and write the project's `telegram.offset_path` with an offset one greater than the highest inspected `update_id` (for example `{ "offset": 12346 }`).

Only enable non-dry `listen` after pending updates are dropped or every project offset is intentionally primed.

## Watchdog

Run manually:

```bash
agent-alert-monitor --config config.yaml watchdog-due
```

A timer should stay silent when nothing is due. In systemd mode the unit runs with `--send-telegram`, so non-empty findings are routed back to each incident's configured project channel as concise stalled messages.

## Ledger inspection

```bash
sqlite3 "$ALERT_MONITOR_STATE_DIR/ledger.sqlite" '.tables'
sqlite3 "$ALERT_MONITOR_STATE_DIR/ledger.sqlite" 'select incident_task_id,status,last_channel_status,last_seen_at from alert_incidents;'
```

## Troubleshooting checklist

- Confirm each Telegram bot is channel admin.
- Confirm every project's configured `telegram.bot_token_env` is in the service environment.
- Confirm each channel id matches the corresponding `projects[].telegram.alert_chat_id`.
- Confirm each Hermes coordinator profile can create a manual card on its configured board before enabling non-dry operation.
- Confirm any provider/cloud readonly smoke commands pass for debugger workers.
