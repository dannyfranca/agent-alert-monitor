# Kanban flow

## Incident cards

New alerts create high-priority cards assigned to the selected project's configured debugger assignee. The idempotency key is `alert-monitor:<project-scoped-fingerprint>:<message-id>` so repeated alerts do not fan out duplicate workers and identical alarms in different projects stay isolated.

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

Only create coder cards for likely code fixes. The coder opens a normal project PR, includes the canonical Kanban task marker in the PR body, posts PR status to the alert channel, and blocks `review-required` until human/reviewer approval.

## No-silence rule

Every blocking, failure, or stalled state must have a matching concise Telegram status message using the configured project prefix. The watchdog exists to detect gaps.
