#!/usr/bin/env bash
set -euo pipefail

PROFILE="${AWS_PROFILE:-alert-monitor-readonly}"
AWS_DIR="${AWS_CONFIG_DIR:-$HOME/.aws}"
mkdir -p "$AWS_DIR"
chmod 700 "$AWS_DIR"

read -r -p "AWS access key id for readonly profile ${PROFILE}: " AWS_ACCESS_KEY_ID
read -r -s -p "AWS secret access key for readonly profile ${PROFILE}: " AWS_SECRET_ACCESS_KEY
printf '\n'
read -r -p "AWS region [us-east-1]: " AWS_REGION
AWS_REGION="${AWS_REGION:-us-east-1}"

umask 077
PY_SCRIPT="$(mktemp)"
trap 'rm -f "$PY_SCRIPT"' EXIT
cat >"$PY_SCRIPT" <<'PY'
from __future__ import annotations

import configparser
import pathlib
import sys

aws_dir = pathlib.Path(sys.argv[1])
profile = sys.argv[2]
region = sys.argv[3]
access_key = sys.stdin.readline().rstrip("\n")
secret_key = sys.stdin.readline().rstrip("\n")
credentials_path = aws_dir / "credentials"
config_path = aws_dir / "config"

credentials = configparser.RawConfigParser()
credentials.read(credentials_path)
if not credentials.has_section(profile):
    credentials.add_section(profile)
credentials.set(profile, "aws_access_key_id", access_key)
credentials.set(profile, "aws_secret_access_key", secret_key)
credentials.remove_option(profile, "aws_session_token")
with credentials_path.open("w", encoding="utf-8") as fh:
    credentials.write(fh)

config = configparser.RawConfigParser()
config.read(config_path)
section = "default" if profile == "default" else f"profile {profile}"
if not config.has_section(section):
    config.add_section(section)
config.set(section, "region", region)
with config_path.open("w", encoding="utf-8") as fh:
    config.write(fh)

credentials_path.chmod(0o600)
config_path.chmod(0o600)
PY

{
  printf '%s\n' "$AWS_ACCESS_KEY_ID"
  printf '%s\n' "$AWS_SECRET_ACCESS_KEY"
} | python3 "$PY_SCRIPT" "$AWS_DIR" "$PROFILE" "$AWS_REGION"

export AWS_SHARED_CREDENTIALS_FILE="$AWS_DIR/credentials"
export AWS_CONFIG_FILE="$AWS_DIR/config"

printf 'Verifying readonly profile %s...\n' "$PROFILE"
aws sts get-caller-identity --profile "$PROFILE" --region "$AWS_REGION" >/dev/null
aws cloudwatch describe-alarms --max-items 1 --profile "$PROFILE" --region "$AWS_REGION" >/dev/null
aws logs describe-log-groups --limit 1 --profile "$PROFILE" --region "$AWS_REGION" >/dev/null
printf 'AWS readonly smoke checks passed for profile %s.\n' "$PROFILE"
printf '\nPersist these environment values anywhere outside this script that must use a non-default/custom AWS location:\n'
printf 'AWS_PROFILE=%q\n' "$PROFILE"
printf 'AWS_REGION=%q\n' "$AWS_REGION"
printf 'AWS_DEFAULT_REGION=%q\n' "$AWS_REGION"
printf 'AWS_SHARED_CREDENTIALS_FILE=%q\n' "$AWS_SHARED_CREDENTIALS_FILE"
printf 'AWS_CONFIG_FILE=%q\n' "$AWS_CONFIG_FILE"
