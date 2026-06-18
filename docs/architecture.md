# Architecture

`agent-alert-monitor` uses an SQS-first intake model. CloudWatch alarm state changes are delivered to an existing dedicated SQS queue, the local Hermes VM polls that queue, and SQLite is the durable local ledger for raw events, normalized alerts, transitions, incidents, and workflow references.

Telegram is an output/status sink and a clearly labeled legacy/manual fallback path. It is not the source of truth for intake, deduplication, recovery correlation, or incident lifecycle decisions.

## Source-of-truth boundary

```text
CloudWatch alarm state changes
  -> existing SNS alert topic or EventBridge alarm-state rule
  -> dedicated SQS Standard intake queue + DLQ
  -> local agent-alert-monitor SQS poller
  -> local SQLite alert ledger
  -> Hermes Kanban incident/debugger/coder workflow
  -> Telegram status/final messages
```

The dedicated SQS queue is the cloud-durable intake boundary. Anything before the queue is producer-owned routing. Anything after the queue is local agent processing. The monitor assumes the queue already exists and that URL/ARN/DLQ values are supplied through local config/env.

## Components

- CloudWatch alarm state changes: emitted through the already-provisioned producer path into a dedicated SQS queue.
- Producer routing: either existing SNS fanout with an SQS subscription or EventBridge CloudWatch alarm-state rules. The local agent does not decide SNS-vs-EventBridge routing.
- SQS source config: supplies queue URL/ARN, DLQ URL/ARN, region, envelope type, polling limits, and delete policy.
- `agent-alert-monitor`: local package that parses SQS-delivered SNS/EventBridge CloudWatch envelopes, writes the ledger, and performs Hermes/Kanban/Telegram side effects.
- SQLite ledger: local source of truth for raw events, deterministic idempotency, transition history, incident state, PR references, and channel evidence.
- Hermes Kanban: execution queue for debugger/coder/reviewer profiles.
- Telegram sink: status/final-message output channel for humans.
- Legacy Telegram fallback: manual-test/backstop intake only; do not use it for durable alert routing once SQS live mode is stable.

## No-public-endpoint design

The VM never needs an inbound webhook, public tunnel, static IP, or NAT rule. It long-polls an existing dedicated SQS queue with narrow consumer credentials. If the local agent is offline, messages remain in SQS until the queue retention window or DLQ policy applies.

## Data flow

1. CloudWatch emits an alarm state change.
2. Existing producer routing delivers the full SNS or EventBridge envelope to the dedicated SQS queue.
3. The local agent runs preflight checks for SQLite, AWS identity/queue access, Hermes/Kanban, and Telegram sink reachability.
4. Only after preflight succeeds, the agent receives an SQS message and stores the raw message durably before side effects.
5. The configured envelope parser normalizes the alert into deterministic event, transition, and incident keys.
6. `alert_events.event_id` deduplicates repeated cloud/SQS deliveries.
7. `alert_transitions.transition_key` deduplicates repeated deliveries for the same alarm state transition.
8. `alert_incidents.project_slug + incident_fingerprint` correlates active ALARM/OK lifecycle state for the correct project and alarm source.
9. The agent creates, correlates, or resolves Kanban incidents after the local commit succeeds.
10. The agent attempts Telegram status/final messages for human visibility, but Telegram is a sink rather than the deletion gate.
11. The agent deletes the SQS message only after durable local commit and required ledger/Kanban side effects complete.

## TicketDoVale source contract

For TicketDoVale production, local config should use these externally supplied values:

- `TICKETDOVALE_AGENT_ALERT_QUEUE_URL`
- `TICKETDOVALE_AGENT_ALERT_QUEUE_ARN`
- `TICKETDOVALE_AGENT_ALERT_DLQ_URL`
- `TICKETDOVALE_AGENT_ALERT_DLQ_ARN`
- `TICKETDOVALE_AWS_ACCOUNT_ID`
- `AWS_REGION` / `AWS_DEFAULT_REGION` set to `sa-east-1` unless the queue output says otherwise
- `ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN`
- `ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID`

The source envelope must match the producer path:

- `aws_sns_cloudwatch_alarm` for SNS topic subscription envelopes with `RawMessageDelivery=false`.
- `aws_eventbridge_cloudwatch_alarm` for direct EventBridge CloudWatch alarm-state payloads.

## Queue retention and replay boundary

SQS retention is the first durability boundary. Use 14 days for the intake queue and DLQ unless an operator intentionally chooses a shorter period. If the local VM can be offline longer than the retention window, add a cloud-side archive/replay mechanism before relying on SQS alone, such as EventBridge archive/replay or another raw-event archive.

DLQ redrive is an operator action, not an automatic local-agent behavior. Inspect with `dlq-inspect`, fix parser/config or producer issues, then redrive only the messages that are safe to replay.

## Ledger v2 assumptions

This project is not live yet, so the v2 ledger optimizes for the final SQS-first target instead of preserving a smooth migration from the older Telegram-first prototype. If `AlertLedger` opens an incompatible older local table shape, it recreates that local table in the current v2/manual-fallback shape rather than carrying forward compatibility-only rows.

The current v2 tables are:

- `alert_sources`: configured local intake sources.
- `alert_events`: one raw SQS/envelope/normalized alert record per deterministic `event_id`.
- `alert_transitions`: one incident-action candidate per deterministic `transition_key`.
- `alert_incidents`: incident state keyed by `incident_id`, with active CloudWatch incidents uniquely constrained by `project_slug + incident_fingerprint`.

Manual/Telegram-oriented helper columns remain in `alert_messages` and `alert_incidents` only as clearly named local fallback support for existing commands/tests while the SQS-first workflow is completed. Manual fallback incidents use `manual:<project>:<scope-and-task-digest>` project slugs so their route-scoped state cannot collide with the canonical SQS/CloudWatch uniqueness rule. They should not be treated as the durable intake boundary.
