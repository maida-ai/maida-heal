#!/usr/bin/env python3
"""Reference customer-side handoff receiver using HMAC and SQLite deduplication."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


class EventRejected(ValueError):
    """The request is unauthenticated or violates the supported event contract."""


def verify_signature(body: bytes, signature: str, secret: str) -> None:
    expected = (
        "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise EventRejected("signature mismatch")


def parse_event(body: bytes) -> dict[str, Any]:
    try:
        event = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EventRejected("body is not valid JSON") from error
    if not isinstance(event, dict):
        raise EventRejected("event must be a JSON object")
    version = event.get("schema_version")
    if not isinstance(version, str) or version.split(".", 1)[0] != "1":
        raise EventRejected("unsupported event schema major")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id.startswith("evt_"):
        raise EventRejected("event_id is missing")
    return event


def release_fields(event: dict[str, Any]) -> tuple[str, int] | None:
    if event.get("type") != "fix.verified":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        raise EventRejected("fix.verified data is missing")
    if data.get("release_mode") != "handoff" or data.get("release_ready") is not True:
        return None
    report = data.get("closure_report")
    if not isinstance(report, dict):
        raise EventRejected("release-ready event has no closed closure report")
    conditions = report.get("conditions")
    if report.get("verdict") != "closed" or not isinstance(conditions, list):
        raise EventRejected("release-ready event has no closed closure report")
    required = {"specific_metrics", "holdouts", "no_new_failures"}
    passed = {
        item.get("name")
        for item in conditions
        if isinstance(item, dict) and item.get("passed") is True
    }
    if passed != required:
        raise EventRejected("release-ready event has incomplete closure conditions")
    finding_id = data.get("finding_id")
    pull_request = data.get("pull_request_number")
    if not isinstance(finding_id, str) or not isinstance(pull_request, int):
        raise EventRejected("handoff needs a finding and pull request")
    return finding_id, pull_request


def consume(database: Path, event: dict[str, Any]) -> str:
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, "
            "occurred_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS release_requests ("
            "event_id TEXT PRIMARY KEY REFERENCES events(event_id), "
            "finding_id TEXT NOT NULL, pull_request_number INTEGER NOT NULL, "
            "status TEXT NOT NULL)"
        )
        inserted = connection.execute(
            "INSERT OR IGNORE INTO events(event_id, event_type, occurred_at) "
            "VALUES (?, ?, ?)",
            (event["event_id"], event.get("type", "unknown"), event["occurred_at"]),
        )
        if inserted.rowcount == 0:
            return f"duplicate event {event['event_id']}: already accepted"
        fields = release_fields(event)
        if fields is None:
            return f"accepted event {event['event_id']}: no release action"
        finding_id, pull_request = fields
        connection.execute(
            "INSERT INTO release_requests("
            "event_id, finding_id, pull_request_number, status) VALUES (?, ?, ?, ?)",
            (event["event_id"], finding_id, pull_request, "pending"),
        )
        return (
            f"queued release request for {finding_id} from PR #{pull_request} "
            f"using event {event['event_id']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--secret-env", default="MAIDA_HEAL_WEBHOOK_SECRET")
    arguments = parser.parse_args()
    secret = os.environ.get(arguments.secret_env, "")
    if not secret:
        print(
            f"missing secret environment variable: {arguments.secret_env}",
            file=sys.stderr,
        )
        return 2
    body = arguments.body.read_bytes()
    try:
        verify_signature(body, arguments.signature, secret)
        event = parse_event(body)
        result = consume(arguments.database, event)
    except EventRejected as error:
        print(str(error), file=sys.stderr)
        return 2
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
