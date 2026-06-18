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

## SQS-first intake operations

SQS is the target source of truth for cloud alert intake. Telegram remains useful as the visible status sink and as a legacy/fallback manual intake path while SQS stabilizes; do not treat Telegram channel history as durable intake state for new SQS-first projects.

### Cloud-side prerequisite boundary

This repository expects the dedicated queue to already exist. Producer routing, queue creation, DLQ creation, and SNS-vs-EventBridge decisions are managed outside the local agent. The local operator only needs the resulting queue outputs and matching envelope type.

Required cloud-side properties for each production source:

1. Dedicated SQS Standard intake queue, not a shared application queue.
2. Dedicated DLQ attached through the intake queue redrive policy (`maxReceiveCount` should match the producer team's retry budget; the design default is 5).
3. Intake and DLQ retention set to the maximum 14-day boundary unless the operator explicitly chooses a shorter window.
4. Existing CloudWatch alarm SNS topic or EventBridge CloudWatch alarm-state rule sends the full SNS/EventBridge envelope to the intake queue.
5. Queue policies allow only the chosen producer (`sns.amazonaws.com` for the source topic ARN or `events.amazonaws.com` for the source rule ARN) to `sqs:SendMessage`.
6. Queue URL/ARN and DLQ URL/ARN are exported for local `.env` values consumed by `config.yaml`.

TicketDoVale output/env var contract:

```dotenv
TICKETDOVALE_AGENT_ALERT_QUEUE_URL=https://sqs.sa-east-1.amazonaws.com/<account-id>/agent-alert-monitor-ticketdovale-prod
TICKETDOVALE_AGENT_ALERT_QUEUE_ARN=arn:aws:sqs:sa-east-1:<account-id>:agent-alert-monitor-ticketdovale-prod
TICKETDOVALE_AGENT_ALERT_DLQ_URL=https://sqs.sa-east-1.amazonaws.com/<account-id>/agent-alert-monitor-ticketdovale-prod-dlq
TICKETDOVALE_AGENT_ALERT_DLQ_ARN=arn:aws:sqs:sa-east-1:<account-id>:agent-alert-monitor-ticketdovale-prod-dlq
TICKETDOVALE_AWS_ACCOUNT_ID=<account-id>
AWS_REGION=sa-east-1
AWS_DEFAULT_REGION=sa-east-1
ALERT_MONITOR_SQS_SOURCE=ticketdovale-prod-alerts
ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN=<status-bot-token>
ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID=<status-chat-id>
```

Use `aws_sns_cloudwatch_alarm` when the queue receives an SNS envelope with `RawMessageDelivery=false`. Use `aws_eventbridge_cloudwatch_alarm` when the queue receives direct EventBridge CloudWatch alarm state-change payloads.

### Local source config

Minimum source config for an existing dedicated queue:

```yaml
sources:
  - name: ticketdovale-prod-alerts
    type: aws_sqs
    queue_url_env: TICKETDOVALE_AGENT_ALERT_QUEUE_URL
    queue_arn_env: TICKETDOVALE_AGENT_ALERT_QUEUE_ARN
    dlq_queue_url_env: TICKETDOVALE_AGENT_ALERT_DLQ_URL
    dlq_queue_arn_env: TICKETDOVALE_AGENT_ALERT_DLQ_ARN
    region: sa-east-1
    envelope: aws_sns_cloudwatch_alarm # or aws_eventbridge_cloudwatch_alarm
    wait_time_seconds: 20
    max_messages: 10
    visibility_timeout_seconds: 300
    delete_policy: after_successful_side_effects
sinks:
  - name: ticketdovale-telegram-status
    type: telegram
    bot_token_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_BOT_TOKEN
    chat_id_env: ALERT_MONITOR_TICKETDOVALE_TELEGRAM_CHAT_ID
```

Required local AWS permissions for health checks, dry-run/DLQ inspection, and live consumption:

```text
sts:GetCallerIdentity
sqs:GetQueueAttributes on the intake queue and DLQ
sqs:GetQueueUrl on the intake queue, if queue name resolution is used
sqs:ReceiveMessage on the intake queue and DLQ
sqs:DeleteMessage on the intake queue, for live delete-after-success mode
sqs:ChangeMessageVisibility on the intake queue, for live retry/backoff mode
```

Additional diagnostic permissions for debugger workers are recommended but not part of SQS deletion safety:

```text
cloudwatch:DescribeAlarms
cloudwatch:DescribeAlarmHistory
cloudwatch:GetMetricData
cloudwatch:GetMetricStatistics
cloudwatch:ListMetrics
logs:DescribeLogGroups
logs:DescribeLogStreams
logs:FilterLogEvents
logs:GetLogEvents
logs:StartQuery
logs:GetQueryResults
logs:StopQuery
```

Additional permissions for operator-approved DLQ redrive/replay are not needed for routine inspection. Grant them only to the operator role used for recovery work:

```text
sqs:StartMessageMoveTask on the DLQ
sqs:CancelMessageMoveTask on the DLQ
sqs:ListMessageMoveTasks on the DLQ
sqs:GetQueueAttributes on the DLQ and destination intake queue
sqs:ReceiveMessage on the DLQ
sqs:DeleteMessage on the DLQ
sqs:SendMessage on the destination intake queue
```

### Health, dry-run, and live commands

Run health before receiving messages in automation. Health checks SQLite, AWS identity, SQS queue attributes, approximate visible/inflight counts, DLQ visible count, Hermes binary/profile/Kanban board, and Telegram sink reachability. Oldest-message age is a CloudWatch metric rather than an SQS queue attribute, so use CloudWatch alarm/metric views for that backlog-age signal:

```bash
agent-alert-monitor --config config.yaml health --source ticketdovale-prod-alerts --json
# Equivalent when config.yaml is in the current directory:
agent-alert-monitor health --source ticketdovale-prod-alerts --json
```

Dry-run parse without deleting messages:

```bash
agent-alert-monitor --config config.yaml sqs-peek --source ticketdovale-prod-alerts --max-messages 10
agent-alert-monitor --config config.yaml sqs-ingest --source ticketdovale-prod-alerts --dry-run
```

`ReceiveMessage` changes SQS visibility and receive counts even when the local command does not delete. Use dry-run only during controlled validation windows, and prefer `health --json` for periodic readiness checks.

Live consumption creates/correlates/resolves incidents and deletes only after durable local commit plus required side effects:

```bash
agent-alert-monitor --config config.yaml sqs-listen --source ticketdovale-prod-alerts --once
agent-alert-monitor --config config.yaml sqs-listen --source ticketdovale-prod-alerts
```

Inspect the DLQ without deleting messages. Output intentionally includes only sanitized summaries, parser errors, message ids, receive counts, sent timestamps, and attribute keys; it omits receipt handles, message bodies, token values, and raw secret-looking fields:

```bash
agent-alert-monitor --config config.yaml dlq-inspect --source ticketdovale-prod-alerts --max-messages 10
# Equivalent when config.yaml is in the current directory:
agent-alert-monitor dlq-inspect --source ticketdovale-prod-alerts --max-messages 10
```

### Manual validation runbook

Use this runbook before enabling the systemd live listener:

1. Confirm the queue receives at least one `ALARM` and one `OK` CloudWatch transition from the chosen producer path.
2. Export queue/DLQ/TicketDoVale env vars and run `health --json`; every blocking dependency should be `ok` before receiving messages.
3. Run `sqs-ingest --dry-run` or `sqs-peek` during a controlled test window and verify the parser emits normalized JSON without deleting the SQS message.
4. Confirm ALARM and OK from the same CloudWatch alarm share the same `incident_fingerprint` and have distinct `transition_key` values.
5. Run `sqs-listen --once` on a test message and verify one ALARM creates or correlates one incident card.
6. Send/replay the matching OK and verify it resolves only the matching open incident and attempts the configured Telegram final/status message.
7. Run `health --json` again and confirm queue/DLQ counts are expected.
8. Run `dlq-inspect --source ticketdovale-prod-alerts --max-messages 10`; any visible messages need parser/config triage before live mode.
9. Confirm the current Telegram alert mirror/status path still posts human-visible messages. It should mirror/status alerts, not be the durable intake source.

### DLQ handling

1. Run `health --json` and fix failed local dependencies before polling the intake queue.
2. Run `dlq-inspect --source <name> --max-messages 10` and identify whether failures are parser/envelope issues, stale test data, or an AWS routing/config mismatch.
3. If messages are safe to replay and still inside the queue retention window, redrive from the DLQ to the intake queue using the AWS console/CLI after fixing the parser/config issue.
4. If messages contain old malformed test payloads, document the decision and purge only the DLQ messages that are known non-production noise.
5. If the local agent may be offline longer than the 14-day SQS retention boundary, add an EventBridge archive or another raw-event archive and replay path before relying on SQS alone.

### systemd operation

The bundled user-service templates use `ALERT_MONITOR_SQS_SOURCE` from `.env` to select the source. For TicketDoVale, set it to `ticketdovale-prod-alerts`; otherwise the sample `.env.example` default points at `sample-api-prod-alerts`. `agent-alert-monitor-sqs-readiness.service` and `agent-alert-monitor-health.timer` are safe to enable for readiness because they do not receive messages. Enable `agent-alert-monitor-sqs-listen.service` only after the manual validation runbook is clean and the human gates below are satisfied.

```bash
./scripts/systemd-install.sh
systemctl --user edit agent-alert-monitor-sqs-readiness.service
systemctl --user edit agent-alert-monitor-health.service
systemctl --user daemon-reload
systemctl --user enable --now agent-alert-monitor-sqs-readiness.service agent-alert-monitor-health.timer agent-alert-monitor-watchdog.timer
journalctl --user -u agent-alert-monitor-health.service -n 100 --no-pager

# After validation/gates, enable live SQS intake:
systemctl --user enable --now agent-alert-monitor-sqs-listen.service
journalctl --user -u agent-alert-monitor-sqs-listen.service -n 100 --no-pager
```

Current SQS readiness service intentionally runs the non-receiving `health --json` check rather than `sqs-peek` or `sqs-ingest`, because `ReceiveMessage` changes SQS visibility/receive counts even in dry-run mode.

### Migration, rollback, and human gates

This project is not production-live until the v2 stack is finalized. Optimize local config and docs for the final SQS-first target rather than preserving smooth Telegram-first compatibility.

Known human gates before production live mode:

- Queue URL/ARN/DLQ outputs are available from the cloud operator.
- AWS credentials with the queue consumer permissions are installed locally and validated.
- Telegram status bot token/chat id are installed locally and can post to the status channel.
- Hermes coordinator/debugger/coder/reviewer profiles and Kanban board slugs exist.
- Parent implementation PRs for SQS intake, health/DLQ, and lifecycle sync are merged.
- Operator approves enabling `agent-alert-monitor-sqs-listen.service`.

Rollback path:

1. Stop the local SQS listener: `systemctl --user stop agent-alert-monitor-sqs-listen.service`.
2. Leave cloud producer routing and the existing Telegram alert mirror intact so human visibility remains available.
3. Inspect local ledger and DLQ state before deleting or replaying anything.
4. Fix config/parser/credential issues, run health and dry-run again, then restart the listener.
5. If rollback lasts longer than SQS retention, rely on the configured cloud archive/replay path; SQS alone cannot recover messages after retention expiry.

Local troubleshooting checklist:

- `health --json` returns nonzero: inspect the failed check name first; the output sanitizes exception details by error class/code.
- `sqs_queue_access` failed: verify region, queue URL/ARN env vars, AWS profile, and `sqs:GetQueueAttributes` permissions.
- `sqs-ingest --dry-run` parses zero useful alerts: verify the configured envelope type matches SNS vs EventBridge delivery.
- SQS backlog grows: confirm the local service is enabled and that Hermes/Kanban/Telegram health checks are green before receiving messages. Use CloudWatch's `ApproximateAgeOfOldestMessage` metric for oldest-message age; it is not returned by SQS `GetQueueAttributes`.
- `sqs_dlq_visible` is nonzero: run `dlq-inspect`, fix parser/config, then redrive or purge with a written operator decision.
- `telegram_sink` failed: Telegram status may be unavailable; investigate the sink so humans keep visibility, but do not treat this as SQS intake loss.

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

- Confirm each SQS source has queue URL/ARN, DLQ URL/ARN, region, and envelope type configured.
- Confirm AWS credentials can run `sts:GetCallerIdentity` and access the intake queue/DLQ attributes before receiving messages.
- Confirm each Telegram status bot is channel admin and that the configured sink chat id env var is present in the service environment.
- Confirm legacy fallback projects still define `telegram.bot_token_env`, `telegram.alert_chat_id`, and `telegram.offset_path` if you intentionally use Telegram polling.
- Confirm each Hermes coordinator profile can create a manual card on its configured board before enabling live SQS operation.
- Confirm any provider/cloud readonly smoke commands pass for debugger workers.
