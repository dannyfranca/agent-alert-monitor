# Agent Alert Monitor

Local Telegram-to-Hermes Kanban incident coordinator for configured alert channels.

This project turns one or more existing Telegram alert channels into durable incident intake paths without exposing the operator's local VM and without making the monitored systems depend on Hermes. Alerts keep flowing to Telegram as they do today; this agent listens locally, records every alert in a SQLite ledger, correlates related alerts per configured project, plans or creates high-priority Kanban incident cards, and emits concise status messages so failures do not disappear silently.

Version: `0.1.0`.
License: MIT.

## Architecture

```text
Alerting system / monitoring provider
  → configured Telegram alert channel(s)
  → local Telegram bot poller/listener
  → alert coordinator profile/session
  → durable alert ledger
  → high-priority Kanban incident cards
  → debugger profile investigates
  → coder PR card only when code fix is likely
  → concise status messages posted back to alert channel
```

Core principle:

```text
Telegram session = correlation and reasoning context
Alert ledger = durable intake/dedupe/recovery state
Kanban = execution state and multi-agent fan-out
Watchdog = no-silence guarantee
```

### High-level flow

```mermaid
flowchart TD
    A[Alerting system alarms] --> B[Configured Telegram alert channel]
    B --> C[Local Telegram bot polling/listening]
    C --> D[Alert coordinator profile/session]
    D --> E[(Alert ledger SQLite)]
    D --> F{New or related alert?}
    F -->|Related| G[Update existing incident card/comment]
    F -->|New| H[Create high-priority Kanban incident]
    H --> I[Debugger profile]
    G --> I
    I --> J{Classification}
    J -->|Self-recovered / transient| K[Post final status and complete incident]
    J -->|Code fix likely| L[Create high-priority coder card]
    J -->|Infra/manual action| M[Block incident and post needed action]
    J -->|Human decision| N[Block incident and post decision request]
    J -->|Missing access/tooling| O[Block incident and post missing prerequisite]
    L --> P[Coder opens/updates PR]
    P --> Q[Post PR status to alert channel]
    R[Watchdog] --> E
    R --> S[Post stalled/failure message if silent]
```

### Sequence: new alert → debugger → resolution

```mermaid
sequenceDiagram
    autonumber
    participant Source as Alert source
    participant TG as Telegram alert channel
    participant Bot as Local bot poller
    participant Coord as alert-coordinator
    participant Ledger as alert ledger
    participant KB as Kanban board
    participant Debug as debugger profile
    participant Coder as coder profile

    Source->>TG: Alert message
    TG->>Bot: channel_post update
    Bot->>Coord: Dispatch channel message into coordinator
    Coord->>Ledger: Store raw message + project-scoped fingerprint
    Coord->>KB: Find/create incident card, priority 1000+
    Coord->>TG: Ack: investigating / correlated
    KB->>Debug: Dispatcher spawns debugger card
    Debug->>TG: Ack: investigation started
    Debug->>Debug: Query logs/metrics/deploy context
    Debug->>KB: Comment evidence + classification

    alt Self-recovered / transient
        Debug->>TG: Final: recovered + evidence
        Debug->>KB: Complete incident
    else Code fix likely
        Debug->>KB: Create high-priority coder card
        Debug->>TG: Update: code fix queued
        KB->>Coder: Dispatcher spawns coder card
        Coder->>KB: Opens PR, blocks review-required
        Coord->>TG: PR opened/status
    else Decision / infra / missing access
        Debug->>TG: Needs action/decision/prereq
        Debug->>KB: Block incident with same concise reason
    end
```

### Incident state machine

```mermaid
stateDiagram-v2
    [*] --> Observed: Telegram channel_post
    Observed --> Correlating: coordinator parses/fingerprints
    Correlating --> Duplicate: same alert already handled
    Correlating --> IncidentOpen: new or active incident
    Duplicate --> [*]
    IncidentOpen --> DebugQueued: Kanban card priority 1000+
    DebugQueued --> Investigating: debugger claimed
    Investigating --> SelfRecovered: metrics/logs normalized
    Investigating --> CodeFixQueued: coder card created
    Investigating --> OpsBlocked: infra/manual action needed
    Investigating --> DecisionBlocked: human/operator decision needed
    Investigating --> AccessBlocked: missing credentials/tooling
    Investigating --> Stalled: watchdog SLA missed
    SelfRecovered --> Done: final channel message posted
    CodeFixQueued --> PROpened: coder opens PR
    PROpened --> AwaitingReview: review-required
    AwaitingReview --> Done: PR merged / incident resolved
    OpsBlocked --> Done: operator resolves and confirms
    DecisionBlocked --> Investigating: operator answers + unblock
    AccessBlocked --> Investigating: access fixed + unblock
    Stalled --> Investigating: reclaimed/unblocked
```

## What is implemented in v0.1.0

- Python package with CLI entry point `agent-alert-monitor`.
- YAML/env configuration loader with multiple project/channel definitions that keeps tokens out of committed files.
- SQLite ledger for raw messages, fingerprints, incident mapping, idempotency, and watchdog state.
- Alert parsing/fingerprinting with stable project-scoped dedupe across noisy metric values.
- Dry-run synthetic alert flow that produces the planned Kanban card and channel message with zero Telegram/provider/Kanban side effects.
- Telegram `getUpdates` poll-once helper with persisted offset.
- Standard concise Telegram message templates.
- Watchdog evaluation for stalled incidents.
- systemd user unit examples for intake and watchdog.
- Public docs for architecture, operations, message templates, and Kanban flow.

## Prerequisites

- Linux host with Python 3.11+.
- Hermes installed locally with Kanban enabled.
- Telegram listener bot token per monitored source. Each listener bot must be an admin of its alert channel so it can receive `channel_post` updates.
- Hermes coordinator/debugger profiles and target Kanban board slugs already created and usable from the local Hermes CLI.
- Optional cloud/provider CLIs configured readonly if debugger workers will inspect logs, metrics, deploys, or traces.
- Optional: `gh` for release/tag workflows if you use GitHub.

## Install locally

```bash
cd agent-alert-monitor
./scripts/install.sh
cp config.example.yaml config.yaml
install -m 600 .env.example .env
```

The installer also pre-creates `./state` with mode `0700`; keep that restrictive mode because the ledger contains production alert metadata.

Edit `config.yaml` and `.env` locally. Do not commit either file.


## Multi-project configuration

`config.yaml` uses a top-level `projects:` list so one install can monitor multiple independent channels. Each entry controls:

- `slug` and `display_name` for project identity and card titles.
- `telegram.bot_token_env`, `telegram.alert_chat_id`, and `telegram.offset_path` for the source channel.
- `hermes.coordinator_profile`, `hermes.kanban_board`, and `hermes.channel_target` for local Hermes routing.
- `kanban.tenant`, `kanban.incident_assignee`, and priorities for generated incident cards.
- `messages.prefix` for visible channel status/final messages.

Example projects in `config.example.yaml`:

- `sample-api`: API/service alerts routed to `sample-api-incidents` and `debugger`.
- `worker-queue`: background worker alerts routed to `worker-incidents` and `worker-debugger`.

Use `--project <slug>` for synthetic tests or one-off polling of a single project. Omit `--project` for `ingest`/`listen` to process all configured projects.

Minimal local smoke test:

```bash
source .venv/bin/activate
set -a; . ./.env; set +a
agent-alert-monitor --config config.yaml --project sample-api synthetic-alert \
  --message-id synthetic-1 \
  --text 'CRITICAL ALARM: Service5xx service=api region=us-east-1' \
  --dry-run
```

The output is JSON. It should include:

- `action: would_create_incident`
- `external_side_effects: false`
- a planned Kanban card assigned to the selected project's debugger profile
- a concise project-prefixed `🔎 ... alert monitor` channel message

## Telegram setup

1. Create a dedicated Telegram listener bot.
   - If alerts are already posted by a bot, do not reuse that alert-posting bot; Telegram does not deliver a bot's own channel posts through `getUpdates`.
   - Reuse is only safe when channel alerts are posted by another actor, such as a human account or a different bot.
2. Add it to the existing application alert channel as an admin.
3. Put each token in local environment only, using the env var named by that project, such as `ALERT_MONITOR_SAMPLE_API_TELEGRAM_BOT_TOKEN=...`.
4. Clear any existing webhook before polling. For the first live run after dry-run testing, intentionally drop stale pending updates for each project bot so old channel posts are not replayed into real Kanban/status side effects:

   ```bash
   python - <<'PY'
   from urllib.parse import urlencode
   from urllib.request import urlopen
   import json, os

   token_envs = [
       "ALERT_MONITOR_SAMPLE_API_TELEGRAM_BOT_TOKEN",
       "ALERT_MONITOR_WORKER_QUEUE_TELEGRAM_BOT_TOKEN",
   ]
   for token_env in token_envs:
       token = os.environ[token_env]
       base = f"https://api.telegram.org/bot{token}"
       print(f"# {token_env}")
       for path, query in [
           ("deleteWebhook", {"drop_pending_updates": "true"}),
           ("getWebhookInfo", {}),
       ]:
           url = f"{base}/{path}"
           if query:
               url = f"{url}?{urlencode(query)}"
           print(json.dumps(json.load(urlopen(url, timeout=15)), indent=2))
   PY
   ```

   `getWebhookInfo` should report an empty `url`; otherwise Telegram will reject `getUpdates` polling with a webhook conflict.
5. For every `projects[]` entry, set `telegram.alert_chat_id`, `hermes.channel_target`, `hermes.kanban_board`, `kanban.tenant`, `kanban.incident_assignee`, and `messages.prefix` in `config.yaml`. Use Hermes board slugs such as `sample-api-incidents`, not SQLite database paths.
6. Run a dry-run synthetic alert for each project before enabling non-dry behavior.
7. If you cannot drop pending updates because you need the backlog for investigation, prime each project's offset intentionally before live mode: page through Telegram `getUpdates` until no pending updates remain, inspect every returned page, and write the configured `telegram.offset_path` in the agent's expected JSON format, for example `{ "offset": 12346 }`, where the value is one greater than the highest inspected `update_id`. Only start non-dry `listen` after every project offset file is in place.

This design uses local polling/listening. It does not require public webhooks, ngrok, reverse SSH tunnels, or inbound router/NAT changes.

## Hermes profile assumptions

Recommended profile split per project:

- `alert-coordinator` or a project-specific coordinator profile: owns correlation, ledger updates, Kanban fan-out, and channel status text.
- `debugger`: first responder for log/metric/deploy investigation.
- `coder`: opens code-fix PRs only when the debugger classifies the incident as code-fix-likely.
- `reviewer`: reviews code/product fit before merge.

The local poller can create Kanban cards through the Hermes CLI in non-dry mode. Before enabling live mode, verify every configured coordinator profile exists and can create cards on its configured board slug. Agent workers should still prefer native Kanban tools when already running inside Hermes.

## Optional provider readonly credentials

Use readonly credentials only. The included AWS helper script prompts locally and writes files with restrictive permissions:

```bash
./scripts/setup-aws-readonly.sh
```

Suggested permission families for a debugger profile:

- `sts:GetCallerIdentity`
- `cloudwatch:DescribeAlarms`, `cloudwatch:GetMetricData`, `cloudwatch:GetMetricStatistics`
- `logs:DescribeLogGroups`, `logs:DescribeLogStreams`, `logs:FilterLogEvents`, `logs:GetLogEvents`

Do not commit cloud/provider credentials or profile files.

## systemd user services

Install example units for the current user:

```bash
./scripts/systemd-install.sh
systemctl --user edit agent-alert-monitor-ingest.service
systemctl --user enable --now agent-alert-monitor-ingest.service agent-alert-monitor-watchdog.timer
```

The install script substitutes the current repo path into the unit files. Run it from the clone path you intend to operate. On headless VMs, ensure the user manager survives logout with `loginctl enable-linger <user>` if that is not already configured.

## Common commands

```bash
# Tests
python -m pytest -q

# Lint if ruff is installed
python -m ruff check .

# Dry-run synthetic alert
agent-alert-monitor --config config.yaml --project sample-api synthetic-alert --text 'ALARM: Service5xx service=api' --dry-run

# Poll Telegram once without creating cards
agent-alert-monitor --config config.yaml ingest --dry-run  # all configured projects
agent-alert-monitor --config config.yaml --project worker-queue ingest --dry-run

# Print watchdog findings as JSON
agent-alert-monitor --config config.yaml watchdog-due
```

## Security model

- No secrets are committed. `.env`, `config.yaml`, local state, and SQLite ledgers are ignored.
- `config.example.yaml` and `.env.example` contain placeholders only.
- Telegram tokens are read from local environment variables named by each project config.
- Cloud/provider access should be readonly and scoped to the triage surfaces debugger workers need.
- Ledger data is local operational state: raw alert text, fingerprints, incident ids, status timestamps, optional PR references. Treat it as sensitive production metadata.
- Rotate or prune ledger state according to your incident retention needs. A simple first policy is to back up then delete resolved rows older than 90 days.

## Troubleshooting

- No Telegram updates: confirm the bot is channel admin and the channel id matches the selected project's `telegram.alert_chat_id`.
- Token error: ensure the project's configured `telegram.bot_token_env` is exported in the same environment running the service.
- Duplicate incidents: inspect the fingerprint in `ledger.sqlite`; fingerprints are scoped by project, and alerts with different alarm/service/region/env intentionally become different incidents.
- Silent incident: run `agent-alert-monitor --config config.yaml watchdog-due` and check whether `last_channel_post_at` is being updated.
- Kanban card not created in non-dry mode: verify Hermes CLI auth/profile can run `hermes kanban create` manually.

## Versioning and upgrades

Use semantic versions. This project starts at `v0.1.0`. Before publishing a new release:

1. Run tests and lint.
2. Run a dry-run synthetic alert.
3. Check that examples still contain no secrets and no private/project-specific names.
4. Commit with Conventional Commits.
5. Tag and push to the already-configured remote:

```bash
git tag -a v0.1.0 -m 'v0.1.0'
git push origin v0.1.0
```

## Release checklist

From a normal clone with an already-configured remote, publish reviewed commits and then create the first release tag:

```bash
git tag -a v0.1.0 -m 'v0.1.0'
git push origin v0.1.0
```
