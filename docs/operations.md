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

Provision the cloud side before local configuration:

1. Create a dedicated SQS Standard intake queue for the monitor, not a shared application queue.
2. Create a dedicated DLQ and attach it through the intake queue redrive policy (`maxReceiveCount` should match the producer team's retry budget; the design default is 5).
3. Set both intake and DLQ retention to the maximum 14-day boundary unless the operator explicitly chooses a shorter window.
4. Wire the existing CloudWatch alarm SNS topic or EventBridge CloudWatch alarm-state rule to send the full SNS/EventBridge envelope to the intake queue.
5. Add queue policies that allow only the chosen producer (`sns.amazonaws.com` for the source topic ARN or `events.amazonaws.com` for the source rule ARN) to `sqs:SendMessage`.
6. Export the intake queue URL/ARN and DLQ URL/ARN into the local `.env` values consumed by `config.yaml`.

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
```

Required local AWS permissions for health checks, dry-run/DLQ inspection, and later live consumption:

```text
sts:GetCallerIdentity
sqs:GetQueueAttributes on the intake queue and DLQ
sqs:ReceiveMessage on the intake queue and DLQ
sqs:DeleteMessage on the intake queue, for live delete-after-success mode
sqs:ChangeMessageVisibility on the intake queue, for live retry/backoff mode
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

Run health before receiving messages in automation. Health checks SQLite, AWS identity, SQS queue attributes, approximate visible/inflight counts, DLQ visible count, Hermes binary/profile/Kanban board, and Telegram sink reachability. Oldest-message age is a CloudWatch metric rather than an SQS queue attribute, so use CloudWatch alarm/metric views for that backlog-age signal:

```bash
agent-alert-monitor --config config.yaml health --source ticketdovale-prod-alerts --json
# Equivalent when config.yaml is in the current directory:
agent-alert-monitor health --source ticketdovale-prod-alerts --json
```

Inspect the DLQ without deleting messages. Output intentionally includes only sanitized summaries, parser errors, message ids, receive counts, sent timestamps, and attribute keys; it omits receipt handles, message bodies, token values, and raw secret-looking fields:

```bash
agent-alert-monitor --config config.yaml dlq-inspect --source ticketdovale-prod-alerts --max-messages 10
# Equivalent when config.yaml is in the current directory:
agent-alert-monitor dlq-inspect --source ticketdovale-prod-alerts --max-messages 10
```

DLQ handling runbook:

1. Run `health --json` and fix failed local dependencies before polling the intake queue.
2. Run `dlq-inspect --source <name> --max-messages 10` and identify whether failures are parser/envelope issues, stale test data, or an AWS routing/config mismatch.
3. If messages are safe to replay and still inside the queue retention window, redrive from the DLQ to the intake queue using the AWS console/CLI after fixing the parser/config issue.
4. If messages contain old malformed test payloads, document the decision and purge only the DLQ messages that are known non-production noise.
5. If the local agent may be offline longer than the 14-day SQS retention boundary, add an EventBridge archive or another raw-event archive and replay path before relying on SQS alone.

The bundled user-service templates use `ALERT_MONITOR_SQS_SOURCE` from `.env` to select the source. `agent-alert-monitor-sqs-readiness.service` and `agent-alert-monitor-health.timer` are safe to enable now because they do not receive messages. `agent-alert-monitor-sqs-listen.service` is installed as the live listener template, but only enable it after live SQS delete-after-side-effects behavior is implemented and reviewed.

```bash
./scripts/systemd-install.sh
systemctl --user edit agent-alert-monitor-sqs-readiness.service
systemctl --user edit agent-alert-monitor-health.service
systemctl --user daemon-reload
systemctl --user enable --now agent-alert-monitor-sqs-readiness.service agent-alert-monitor-health.timer agent-alert-monitor-watchdog.timer
journalctl --user -u agent-alert-monitor-health.service -n 100 --no-pager
```

Current SQS readiness service intentionally runs the non-receiving `health --json` check rather than `sqs-peek` or `sqs-ingest`, because `ReceiveMessage` changes SQS visibility/receive counts even in dry-run mode. The separate `agent-alert-monitor-sqs-listen.service` template is for future live consumption and should remain disabled until the live command no longer fails fast.

Local troubleshooting checklist:

- `health --json` returns nonzero: inspect the failed check name first; the output sanitizes exception details by error class/code.
- `sqs_queue_access` failed: verify region, queue URL/ARN env vars, AWS profile, and `sqs:GetQueueAttributes` permissions.
- SQS backlog grows: confirm the local service is enabled and that Hermes/Kanban/Telegram health checks are green before receiving messages. Use CloudWatch's `ApproximateAgeOfOldestMessage` metric for oldest-message age; it is not returned by SQS `GetQueueAttributes`.
- `sqs_dlq_visible` is nonzero: run `dlq-inspect`, fix parser/config, then redrive or purge with a written operator decision.
- `telegram_sink` failed: Telegram status may be unavailable; do not silently close incidents that require final channel evidence.

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
