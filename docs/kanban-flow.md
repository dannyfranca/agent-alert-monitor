# Kanban flow

## Incident cards

SQS CloudWatch `ALARM` transitions create or correlate high-priority cards assigned to the selected project's configured debugger assignee. The local ledger, not Kanban text, owns durable idempotency and correlation:

- `event_id` prevents processing the same SQS/SNS/EventBridge delivery twice.
- `transition_key` prevents duplicate incident actions for the same CloudWatch state transition.
- `incident_fingerprint` correlates ALARM/OK lifecycle state for the same alarm source.

Default priorities are configurable per project. The example config uses:

- normal alert: `1000`
- critical/customer-impacting outage: `2000`

## Debugger classification

Debugger workers should classify exactly one final state:

- `self-recovered/transient`
- `code-fix-likely`
- `infra-ops-needed`
- `human-decision-needed`
- `missing-access/tooling`
- `false-positive/noise`

`still-investigating` is interim only.

## Coder handoff

Only create coder cards for likely code fixes. The coder opens a normal project PR, includes the canonical Kanban task marker in the PR body, posts PR status back to the incident lifecycle, and blocks `review-required` until human/reviewer approval.

## Telegram status output

Telegram is a status/final-message sink. It should show investigating, correlated, PR-opened, blocked, and resolved messages for humans, but it is not the source of truth for new SQS-first intake, dedupe, recovery matching, or lifecycle decisions.

## Legacy/manual fallback

Telegram polling may remain as a clearly named manual/fallback path for tests or emergency use. Do not let fallback Telegram message ids collide with SQS CloudWatch incidents; fallback state should stay route-scoped and separate from SQS `event_id`/`transition_key`/`incident_fingerprint` records.

## No-silence rule

Every blocking, failure, or stalled state must have a matching concise Telegram status message using the configured project prefix. The watchdog exists to detect gaps.
