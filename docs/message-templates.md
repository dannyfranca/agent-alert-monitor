# Message templates

All channel updates are intentionally short and predictable. They start with an emoji and the configured project `messages.prefix`, include status, and include the incident id when known. The default prefix is `Alert monitor`; examples below use project-specific prefixes.

## New incident

```text
🔎 Sample API alert monitor
Status: investigating
Incident: t_xxxxxxxx
Signal: <alarm/service/severity>
Next: debugger is checking logs/metrics.
```

## Correlated alert

```text
🔁 Worker Queue alert monitor
Status: correlated with existing incident
Incident: t_xxxxxxxx
Signal: <new alert summary>
Next: debugger context updated.
```

## Stalled

```text
🚨 Sample API alert monitor stalled
Status: no update within SLA
Incident: t_xxxxxxxx
Last seen: <age/status>
Next: watchdog will reclaim/escalate unless progress resumes.
```

See `src/agent_alert_monitor/message_templates.py` for all v0.1.0 templates.
