# Architecture

Configured alert sources continue to land in one or more Telegram alert channels. This agent runs locally on the Hermes VM and consumes Telegram updates through bot polling/listening; no public HTTP endpoint is required.

## Components

- Telegram bot(s): channel admins that can read `channel_post` updates and post concise status text.
- `agent-alert-monitor`: local package that parses messages, writes the ledger, correlates alerts per configured project, and plans or creates Kanban incidents.
- YAML config: top-level `projects[]` entries for project slug/display name, Telegram source, Hermes profile/board, Kanban tenant/assignee, priorities, and message prefix.
- SQLite ledger: durable local state for raw alert messages, project-scoped fingerprints, incident ids, status timestamps, and watchdog decisions.
- Hermes Kanban: execution queue for debugger/coder/reviewer profiles.
- Watchdog: periodic no-silence check for stalled intake/debugger/coder states.

## No-public-endpoint design

The VM never needs an inbound webhook. Telegram is the already-public delivery surface. The local poller asks Telegram for updates using bot tokens stored only in the local environment.

## Data flow

1. Telegram update arrives for a configured project/channel.
2. Agent filters to that project's `telegram.alert_chat_id`.
3. Raw message and normalized fields are stored in SQLite with project-scoped identity.
4. Fingerprint determines duplicate/correlated/new status inside that project.
5. New incidents become high-priority debugger Kanban cards on the configured board/tenant.
6. Channel receives an acknowledgement or correlation status with the configured message prefix.
7. Watchdog emits a stalled/failure message if progress becomes silent.

## Ledger v2 migration assumptions

The local SQLite ledger is migrated in place when `AlertLedger` opens it. Existing Telegram
`alert_messages` rows keep their scoped message idempotency, while legacy `alert_incidents` rows are
copied into the v2 `alert_incidents` shape with deterministic `legacy:<digest>` incident ids and
synthetic `legacy:<scope>:<task>:first|last` event references. Legacy incident rows keep their
original `incident_scope`, `incident_task_id`, and `fingerprint` columns so older Telegram/watchdog
code can continue to read and update them.

Cloud/SQS intake uses the new v2 tables:

- `alert_sources` records configured intake sources.
- `alert_events` stores one raw SQS/envelope/normalized alert record per deterministic `event_id`.
- `alert_transitions` stores one incident-action candidate per deterministic `transition_key`.
- `alert_incidents` stores v2 incident state keyed by `incident_id`, with active CloudWatch incidents
  uniquely constrained by `project_slug + incident_fingerprint`.

The active incident uniqueness rule is intended for v2 CloudWatch/SQS incidents. Migrated Telegram
incidents receive a legacy-scoped `project_slug` so existing multi-route Telegram incidents with the
same fingerprint are preserved safely instead of being collapsed during migration.
