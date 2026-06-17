# Architecture

`agent-alert-monitor` is moving to an SQS-first intake model. CloudWatch alarm state changes are delivered to a dedicated SQS queue, the local Hermes VM polls that queue, and SQLite is the durable local ledger for raw events, normalized alerts, transitions, and incident state.

Telegram is an output/status sink only. It is not the source of truth for intake, deduplication, recovery correlation, or incident lifecycle decisions.

## Components

- CloudWatch alarm state changes: emitted through the already-provisioned producer path into a dedicated SQS queue.
- SQS source config: supplies queue URL/ARN, region, envelope type, polling limits, and delete policy.
- `agent-alert-monitor`: local package that parses SQS-delivered SNS/EventBridge CloudWatch envelopes, writes the ledger, and plans later Hermes/Kanban/Telegram side effects.
- SQLite ledger: local source of truth for raw events, deterministic idempotency, transition history, and incident state.
- Hermes Kanban: later execution queue for debugger/coder/reviewer profiles.
- Telegram sink: later status/final-message output channel for humans.

## No-public-endpoint design

The VM never needs an inbound webhook, public tunnel, static IP, or NAT rule. It long-polls an existing dedicated SQS queue with narrow consumer credentials. If the local agent is offline, messages remain in SQS until the retention window or DLQ policy applies.

## Data flow

1. CloudWatch emits an alarm state change.
2. Existing producer routing delivers the event to the dedicated SQS queue.
3. The local agent receives an SQS message and stores the raw message durably before side effects.
4. The configured envelope parser normalizes the alert into deterministic event, transition, and incident keys.
5. `alert_events.event_id` deduplicates repeated cloud/SQS deliveries.
6. `alert_transitions.transition_key` deduplicates repeated deliveries for the same alarm state transition.
7. `alert_incidents.project_slug + incident_fingerprint` correlates active ALARM/OK lifecycle state for the correct project and alarm source.
8. Later integration PRs will create/update Kanban incidents and send Telegram status messages after the local commit succeeds, then delete from SQS according to the configured policy.

## Ledger v2 assumptions

This project is not live yet, so the v2 ledger optimizes for the final SQS-first target instead of preserving a smooth migration from the older Telegram-first prototype. If `AlertLedger` opens an incompatible older local table shape, it recreates that local table in the current v2/manual-fallback shape rather than carrying forward compatibility-only rows.

The current v2 tables are:

- `alert_sources`: configured local intake sources.
- `alert_events`: one raw SQS/envelope/normalized alert record per deterministic `event_id`.
- `alert_transitions`: one incident-action candidate per deterministic `transition_key`.
- `alert_incidents`: incident state keyed by `incident_id`, with active CloudWatch incidents uniquely constrained by `project_slug + incident_fingerprint`.

Manual/Telegram-oriented helper columns remain in `alert_messages` and `alert_incidents` only as clearly named local fallback support for existing commands/tests while the SQS-first workflow is completed. Manual fallback incidents use `manual:<project>:<scope-and-task-digest>` project slugs so their route-scoped state cannot collide with the canonical SQS/CloudWatch uniqueness rule. They should not be treated as the durable intake boundary.
