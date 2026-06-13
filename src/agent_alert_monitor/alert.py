from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedAlert:
    alarm_name: str
    service: str | None
    severity: str
    state: str
    region: str | None
    environment: str | None
    summary: str


_KEY_VALUE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*)=([^\s,;]+)")


def parse_alert_text(raw_text: str) -> ParsedAlert:
    text = " ".join(raw_text.strip().split())
    pairs = {k.lower(): v.strip("\"'") for k, v in _KEY_VALUE_RE.findall(text)}
    state_match = re.search(r"\b(ALARM|OK|INSUFFICIENT_DATA|RECOVERY|RESOLVED)\b", text, re.I)
    state = (pairs.get("state") or (state_match.group(1) if state_match else "ALARM")).upper()
    severity = (
        "critical" if re.search(r"\b(CRITICAL|P0|SEV[ -]?1|OUTAGE)\b", text, re.I) else "normal"
    )
    service = pairs.get("service") or pairs.get("svc")
    region = pairs.get("region") or pairs.get("aws_region")
    environment = pairs.get("env") or pairs.get("environment") or "prod"

    alarm_match = re.search(r"(?:ALARM|OK|RECOVERY|RESOLVED)[: ]+([A-Za-z0-9_.:/-]+)", text, re.I)
    alarm_name = (
        pairs.get("alarm")
        or pairs.get("alarm_name")
        or (alarm_match.group(1) if alarm_match else "unknown-alert")
    )
    alarm_name = alarm_name.strip("\"'")

    return ParsedAlert(
        alarm_name=alarm_name,
        service=service,
        severity=severity,
        state=state,
        region=region,
        environment=environment,
        summary=f"{alarm_name} {service or 'unknown-service'} {severity}",
    )


def fingerprint_alert(parsed: ParsedAlert) -> str:
    stable = "|".join(
        [
            parsed.alarm_name.lower(),
            (parsed.service or "").lower(),
            (parsed.region or "").lower(),
            (parsed.environment or "").lower(),
        ]
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]
