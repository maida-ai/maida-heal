"""Small structured stderr logger for unattended operation."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TextIO

Clock = Callable[[], datetime]


class StructuredLogger:
    """Write one stable JSON object per line without serializing exceptions."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.stream = stream or sys.stderr
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def write(
        self,
        level: str,
        event: str,
        message: str,
        **fields: object,
    ) -> None:
        payload = {
            "schema_version": "1.0.0",
            "timestamp": self.clock().isoformat(),
            "level": level,
            "event": event,
            "message": message,
            **fields,
        }
        self.stream.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        )
        self.stream.flush()

    def event_delivery(self, entry: dict[str, object]) -> None:
        self.write(
            str(entry.get("level", "error")),
            str(entry.get("event", "event.delivery_failed")),
            "Event delivery failed; the durable outbox will retry.",
            **{
                key: value
                for key, value in entry.items()
                if key not in {"level", "event"}
            },
        )
