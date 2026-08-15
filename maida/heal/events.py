"""Durable, payload-free event outbox with local and webhook delivery.

The local JSONL journal is authoritative and always enabled. Webhook delivery is
at-least-once: a process can stop after the receiver accepts an event but before the
delivery receipt is persisted, so consumers must deduplicate by ``event_id``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from maida.heal.models import (
    EventConfig,
    EventEnvelope,
    EventType,
    Finding,
    GitHubSinkConfig,
    JsonlSinkConfig,
    WebhookSinkConfig,
    jsonable,
)
from maida.heal.state import read_json, write_json


@dataclass(frozen=True)
class DeliveryResponse:
    status_code: int


class WebhookTransport(Protocol):
    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        timeout: float,
    ) -> DeliveryResponse: ...


class URLWebhookTransport:
    """Small standard-library HTTP transport with no ambient authentication."""

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        timeout: float,
    ) -> DeliveryResponse:
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                return DeliveryResponse(response.status)
        except HTTPError as error:
            return DeliveryResponse(error.code)
        except (URLError, TimeoutError, OSError) as error:
            raise ConnectionError("webhook delivery failed") from error


@dataclass(frozen=True)
class FlushResult:
    delivered: int
    failed: int


Log = Callable[[dict[str, object]], None]
Sleep = Callable[[float], None]


class EventJournal:
    """Persist events before delivery and replay undelivered work on restart."""

    def __init__(
        self,
        project_root: Path,
        config: EventConfig,
        *,
        environ: Mapping[str, str] | None = None,
        transport: WebhookTransport | None = None,
        sleep: Sleep = time.sleep,
        log: Log | None = None,
    ) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.config = config
        self.environ = dict(os.environ if environ is None else environ)
        self.transport = transport or URLWebhookTransport()
        self.sleep = sleep
        self.log: Log
        if log is None:
            from maida.heal.structured_log import StructuredLogger

            self.log = StructuredLogger().event_delivery
        else:
            self.log = log
        self.root = self.project_root / ".maida-heal" / "events"
        self.outbox = self.root / "outbox"
        self.receipts = self.root / "receipts"
        self._jsonl_id_cache: dict[Path, set[str]] = {}

    def queue(self, event: EventEnvelope) -> Path:
        """Idempotently persist one logical event before any sink is contacted."""
        path = self.outbox / f"{event.event_id}.json"
        if path.exists():
            stored = EventEnvelope.model_validate(read_json(path))
            if stored != event:
                raise ValueError(f"event ID collision for {event.event_id}")
            return path
        write_json(path, jsonable(event))
        return path

    def contains(self, event_id: str) -> bool:
        """Return whether a valid immutable snapshot is already in the outbox."""
        path = self.outbox / f"{event_id}.json"
        if not path.exists():
            return False
        stored = EventEnvelope.model_validate(read_json(path))
        if stored.event_id != event_id:
            raise ValueError(f"event outbox path does not match {event_id}")
        return True

    def _events(self) -> list[EventEnvelope]:
        if not self.outbox.exists():
            return []
        return [
            EventEnvelope.model_validate(read_json(path))
            for path in sorted(self.outbox.glob("evt_*.json"))
        ]

    def _sink_id(
        self, sink: JsonlSinkConfig | WebhookSinkConfig | GitHubSinkConfig, index: int
    ) -> str:
        material = json.dumps(
            sink.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(material.encode()).hexdigest()[:12]
        return f"{index:02d}-{sink.kind}-{digest}"

    def _receipt(self, event: EventEnvelope, sink_id: str) -> Path:
        return self.receipts / event.event_id / f"{sink_id}.json"

    def _write_receipt(
        self, event: EventEnvelope, sink_id: str, detail: dict[str, object]
    ) -> None:
        write_json(
            self._receipt(event, sink_id),
            {"event_id": event.event_id, "sink": sink_id, **detail},
        )

    def _jsonl_path(self, sink: JsonlSinkConfig) -> Path:
        requested = Path(sink.path).expanduser()
        return requested if requested.is_absolute() else self.project_root / requested

    def _jsonl_ids(self, path: Path) -> set[str]:
        if not path.is_file():
            return set()
        identifiers: set[str] = set()
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                payload = json.loads(line)
                if isinstance(payload, dict) and isinstance(
                    payload.get("event_id"), str
                ):
                    identifiers.add(payload["event_id"])
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid event JSONL journal {path}") from error
        return identifiers

    def _deliver_jsonl(
        self, event: EventEnvelope, sink: JsonlSinkConfig, sink_id: str
    ) -> bool:
        path = self._jsonl_path(sink)
        path.parent.mkdir(parents=True, exist_ok=True)
        identifiers = self._jsonl_id_cache.get(path)
        if identifiers is None:
            identifiers = self._jsonl_ids(path)
            self._jsonl_id_cache[path] = identifiers
        if event.event_id not in identifiers:
            line = json.dumps(
                jsonable(event),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            identifiers.add(event.event_id)
        self._write_receipt(event, sink_id, {"status": "delivered", "path": str(path)})
        return True

    def _deliver_webhook(
        self, event: EventEnvelope, sink: WebhookSinkConfig, sink_id: str
    ) -> bool:
        secret = self.environ.get(sink.secret_env, "")
        if not secret:
            self.log(
                {
                    "level": "error",
                    "event": "event.delivery_failed",
                    "event_id": event.event_id,
                    "sink": sink_id,
                    "error": "webhook secret environment variable is absent",
                }
            )
            return False
        body = json.dumps(
            jsonable(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "maida-heal-event/1.0",
            "X-Maida-Heal-Event-ID": event.event_id,
            "X-Maida-Heal-Signature": f"sha256={signature}",
        }
        for attempt in range(1, sink.max_attempts + 1):
            try:
                response = self.transport.post(
                    sink.url,
                    body,
                    headers,
                    timeout=sink.timeout_seconds,
                )
                if 200 <= response.status_code < 300:
                    self._write_receipt(
                        event,
                        sink_id,
                        {
                            "status": "delivered",
                            "http_status": response.status_code,
                            "attempts": attempt,
                        },
                    )
                    return True
                error = f"HTTP {response.status_code}"
            except Exception:  # Delivery is isolated from the loop by contract.
                error = "transport failure"
            if attempt < sink.max_attempts:
                self.sleep(
                    min(
                        sink.initial_backoff_seconds * (2 ** (attempt - 1)),
                        30.0,
                    )
                )
        self.log(
            {
                "level": "error",
                "event": "event.delivery_failed",
                "event_id": event.event_id,
                "sink": sink_id,
                "error": error,
                "attempts": sink.max_attempts,
            }
        )
        return False

    def _deliver_github(self, event: EventEnvelope, sink_id: str) -> bool:
        # PR creation and closure comments are performed in their domain operations.
        # This receipt records that the generic event dispatcher has no second GitHub
        # mutation to perform, avoiding duplicate comments on restart.
        self._write_receipt(
            event, sink_id, {"status": "domain_surface", "event_type": event.type.value}
        )
        return True

    def flush(self) -> FlushResult:
        """Try every undelivered sink; failures remain queued for a later cycle."""
        delivered = 0
        failed_events: set[str] = set()
        for event in self._events():
            for index, sink in enumerate(self.config.sinks):
                sink_id = self._sink_id(sink, index)
                if self._receipt(event, sink_id).is_file():
                    continue
                try:
                    if isinstance(sink, JsonlSinkConfig):
                        success = self._deliver_jsonl(event, sink, sink_id)
                    elif isinstance(sink, WebhookSinkConfig):
                        success = self._deliver_webhook(event, sink, sink_id)
                    else:
                        success = self._deliver_github(event, sink_id)
                except Exception:
                    success = False
                    self.log(
                        {
                            "level": "error",
                            "event": "event.delivery_failed",
                            "event_id": event.event_id,
                            "sink": sink_id,
                            "error": "local delivery failure",
                        }
                    )
                if success:
                    delivered += 1
                else:
                    failed_events.add(event.event_id)
        return FlushResult(delivered=delivered, failed=len(failed_events))

    def pending_count(self) -> int:
        """Return logical events with at least one sink lacking a receipt."""
        pending = 0
        for event in self._events():
            if any(
                not self._receipt(event, self._sink_id(sink, index)).is_file()
                for index, sink in enumerate(self.config.sinks)
            ):
                pending += 1
        return pending


def queue_finding_events(
    project_root: Path,
    config: EventConfig,
    finding: Finding,
) -> tuple[str, ...]:
    """Project append-only finding history into idempotent public events."""
    journal = EventJournal(project_root, config)
    queued: list[str] = []
    metrics = list(finding.metric_names)
    run_ids = list(
        dict.fromkeys(
            run_id for failure in finding.metric_failures for run_id in failure.run_ids
        )
    )
    evidence = list(
        dict.fromkeys(item.evidence_pointer for item in finding.metric_failures)
    )
    for index, history in enumerate(finding.history):
        event_type = {
            "finding_opened": "finding.opened",
            "evidence_attached": "finding.evidence_added",
            "fix_proposed": "fix.proposed",
            "fix_rejected": "fix.rejected",
            "finding_expired": "fix.expired",
        }.get(history.action)
        if event_type is None:
            continue
        if event_type == "fix.proposed" and (
            not finding.attempts
            or finding.attempts[-1].outcome not in {"proposed", "verified"}
        ):
            continue
        if event_type == "finding.opened":
            data: dict[str, object] = {
                "finding_id": finding.id,
                "stream": finding.stream,
                "stream_id": finding.stream_id,
                "source": finding.source.value,
                "metrics": metrics,
                "run_ids": run_ids,
                "evidence_pointers": evidence,
            }
        elif event_type == "finding.evidence_added":
            data = {
                "finding_id": finding.id,
                "stream_id": finding.stream_id,
                "metrics": metrics,
                "run_ids": run_ids,
                "evidence_pointers": evidence,
            }
        elif event_type == "fix.proposed":
            attempt = finding.attempts[-1]
            data = {
                "finding_id": finding.id,
                "attempt": attempt.number,
                "branch": attempt.branch,
                "changed_paths": attempt.changed_paths,
                "diff_lines": attempt.diff_lines,
                "pull_request_number": attempt.pull_request_number,
                "pull_request_url": attempt.pull_request_url,
            }
        elif event_type == "fix.rejected":
            rejected_attempt = finding.attempts[-1] if finding.attempts else None
            data = {
                "finding_id": finding.id,
                "attempt": rejected_attempt.number if rejected_attempt else None,
                "reason": history.detail,
                "changed_paths": (
                    rejected_attempt.changed_paths if rejected_attempt else []
                ),
            }
        else:
            data = {
                "finding_id": finding.id,
                "reason": history.detail,
                "attempts": len(finding.attempts),
            }
        typed = EventEnvelope.create(
            event_type=EventType(event_type),
            occurred_at=history.timestamp,
            dedupe_key=(
                f"{finding.id}:history:{index}:{history.action}:"
                f"{history.timestamp.isoformat()}"
            ),
            stream_id=finding.stream_id,
            finding_id=finding.id,
            data=data,
        )
        # History projection uses the finding's current cumulative evidence. Once an
        # event has been persisted, that original snapshot is immutable; a later
        # evidence update must not rewrite the opened event or an older evidence
        # event. The new history row receives its own deterministic event ID below.
        if not journal.contains(typed.event_id):
            journal.queue(typed)
            queued.append(typed.event_id)
    return tuple(queued)
