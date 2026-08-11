from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from maida_heal.detection import persist_failures
from maida_heal.events import DeliveryResponse, EventJournal
from maida_heal.models import (
    ClosureCondition,
    ClosureReport,
    EventConfig,
    EventEnvelope,
    EventType,
    FindingSource,
    HealConfig,
    JsonlSinkConfig,
    LangfuseConfig,
    MetricFailure,
    StreamConfig,
    WebhookSinkConfig,
)
from maida_heal.state import StateStore

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def opened_event() -> EventEnvelope:
    return EventEnvelope.create(
        event_type=EventType.FINDING_OPENED,
        occurred_at=NOW,
        dedupe_key="mh-20260811-0123456789:opened",
        stream_id="support-agent",
        finding_id="mh-20260811-0123456789",
        data={
            "finding_id": "mh-20260811-0123456789",
            "stream": "Support agent",
            "stream_id": "support-agent",
            "source": "shadow_watch",
            "metrics": ["step_count"],
            "run_ids": ["a" * 32],
            "evidence_pointers": [".maida-heal/reports/report.json"],
        },
    )


def test_jsonl_is_always_on_and_logical_events_are_written_once(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path, EventConfig())
    event = opened_event()

    journal.queue(event)
    journal.queue(event)
    first = journal.flush()
    second = journal.flush()

    lines = (tmp_path / ".maida-heal" / "events.jsonl").read_text().splitlines()
    assert [json.loads(line)["event_id"] for line in lines] == [event.event_id]
    assert first.delivered == 1
    assert second.delivered == 0
    assert journal.pending_count() == 0


class RecordingTransport:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        *,
        timeout: float,
    ) -> DeliveryResponse:
        del timeout
        self.calls.append((url, body, headers))
        return DeliveryResponse(self.statuses.pop(0))


def test_webhook_retries_use_one_event_id_and_correct_hmac(
    tmp_path: Path, monkeypatch: object
) -> None:
    del monkeypatch
    secret = "fixture-webhook-secret"
    transport = RecordingTransport([503, 202])
    sleeps: list[float] = []
    config = EventConfig(
        sinks=[
            JsonlSinkConfig(),
            WebhookSinkConfig(
                url="https://hooks.example.test/maida-heal",
                secret_env="MAIDA_HEAL_WEBHOOK_SECRET",
                max_attempts=3,
                initial_backoff_seconds=0.25,
            ),
        ]
    )
    journal = EventJournal(
        tmp_path,
        config,
        environ={"MAIDA_HEAL_WEBHOOK_SECRET": secret},
        transport=transport,
        sleep=sleeps.append,
    )
    event = opened_event()
    journal.queue(event)

    result = journal.flush()

    assert result.failed == 0
    assert len(transport.calls) == 2
    assert sleeps == [0.25]
    bodies = {call[1] for call in transport.calls}
    assert len(bodies) == 1
    body = bodies.pop()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    for _, _, headers in transport.calls:
        assert headers["X-Maida-Heal-Event-ID"] == event.event_id
        assert headers["X-Maida-Heal-Signature"] == f"sha256={expected}"


def test_webhook_failure_never_raises_or_loses_the_outbox_event(tmp_path: Path) -> None:
    transport = RecordingTransport([500, 500])
    config = EventConfig(
        sinks=[
            WebhookSinkConfig(
                url="https://hooks.example.test/maida-heal",
                secret_env="MAIDA_HEAL_WEBHOOK_SECRET",
                max_attempts=2,
                initial_backoff_seconds=0,
            )
        ]
    )
    journal = EventJournal(
        tmp_path,
        config,
        environ={"MAIDA_HEAL_WEBHOOK_SECRET": "secret"},
        transport=transport,
        sleep=lambda _seconds: None,
    )
    journal.queue(opened_event())

    result = journal.flush()

    assert result.failed == 1
    assert journal.pending_count() == 1
    assert (tmp_path / ".maida-heal" / "events.jsonl").is_file()


def test_webhook_exponential_backoff_is_capped(tmp_path: Path) -> None:
    transport = RecordingTransport([500, 500, 202])
    sleeps: list[float] = []
    config = EventConfig(
        sinks=[
            WebhookSinkConfig(
                url="https://hooks.example.test/maida-heal",
                secret_env="MAIDA_HEAL_WEBHOOK_SECRET",
                max_attempts=3,
                initial_backoff_seconds=20,
            )
        ]
    )
    journal = EventJournal(
        tmp_path,
        config,
        environ={"MAIDA_HEAL_WEBHOOK_SECRET": "secret"},
        transport=transport,
        sleep=sleeps.append,
    )
    journal.queue(opened_event())

    result = journal.flush()

    assert result.failed == 0
    assert sleeps == [20, 30]


def test_event_data_models_forbid_payload_shaped_extra_fields() -> None:
    payload = opened_event().model_dump(mode="json")
    payload["data"]["content"] = "SENTINEL_CUSTOMER_PII"

    try:
        EventEnvelope.model_validate(payload)
    except ValueError as error:
        assert "Extra inputs are not permitted" in str(error)
    else:  # pragma: no cover - assertion clarity
        raise AssertionError("event data unexpectedly accepted payload content")


def test_every_published_event_type_validates_against_versioned_schema() -> None:
    closure = ClosureReport(
        finding_id="mh-20260811-0123456789",
        generated_at=NOW,
        maida_report_version="2.0.0",
        verdict="closed",
        conditions=[
            ClosureCondition(
                name=name,
                passed=True,
                evidence_pointers=[f".maida/{name}.json"],
                detail="Fixture condition passed.",
            )
            for name in ("specific_metrics", "holdouts", "no_new_failures")
        ],
    )
    common = {
        "finding_id": "mh-20260811-0123456789",
    }
    samples: dict[EventType, dict[str, object]] = {
        EventType.FINDING_OPENED: opened_event().data.model_dump(mode="json"),
        EventType.FINDING_EVIDENCE_ADDED: {
            **common,
            "stream_id": "support-agent",
            "metrics": ["step_count"],
            "run_ids": ["b" * 32],
            "evidence_pointers": [".maida-heal/reports/next.json"],
        },
        EventType.FIX_PROPOSED: {
            **common,
            "attempt": 1,
            "branch": "maida-heal/mh-20260811-0123456789-a1",
            "changed_paths": ["prompts/agent.md"],
            "diff_lines": 2,
            "pull_request_number": 17,
            "pull_request_url": "https://github.example.test/pull/17",
        },
        EventType.FIX_REJECTED: {
            **common,
            "attempt": 1,
            "reason": "Protected path changed.",
            "changed_paths": [".maida/policy.yaml"],
        },
        EventType.FIX_VERIFIED: {
            **common,
            "pull_request_number": 17,
            "release_mode": "handoff",
            "release_ready": True,
            "closure_report": closure.model_dump(mode="json"),
        },
        EventType.FIX_EXPIRED: {
            **common,
            "reason": "Attempt budget exhausted.",
            "attempts": 2,
        },
        EventType.LOOP_PAUSED: {
            "actor": "operator@example.test",
            "scope": "all",
        },
        EventType.LOOP_RESUMED: {
            "actor": "operator@example.test",
            "scope": "all",
        },
        EventType.ROLLBACK_OPENED: {
            **common,
            "prior_finding_id": "mh-20260810-fedcba9876",
            "pull_request_url": "https://github.example.test/pull/18",
            "branch": "maida-heal/revert-mh-20260811-0123456789",
        },
    }
    schema = json.loads((ROOT / "schemas" / "event-1.0.0.schema.json").read_text())
    validator = Draft202012Validator(schema)

    for index, (event_type, data) in enumerate(samples.items()):
        event = EventEnvelope.create(
            event_type=event_type,
            occurred_at=NOW,
            dedupe_key=f"schema-sample-{index}",
            finding_id=(common["finding_id"] if "finding_id" in data else None),
            stream_id=(str(data["stream_id"]) if "stream_id" in data else None),
            data=data,
        )
        validator.validate(event.model_dump(mode="json"))


def test_published_schema_rejects_data_for_a_different_event_type() -> None:
    payload = opened_event().model_dump(mode="json")
    payload["type"] = EventType.FIX_PROPOSED.value
    schema = json.loads((ROOT / "schemas" / "event-1.0.0.schema.json").read_text())

    errors = list(Draft202012Validator(schema).iter_errors(payload))

    assert errors


def test_new_evidence_preserves_prior_event_snapshots_and_emits_once(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path)
    stream = StreamConfig(
        id="support-agent-aabbccdd",
        name="Support agent",
        grouping="trace_name",
        grouping_key="traceName",
        grouping_value_hash="aabbccddeeff",
        trace_names=["support-agent"],
    )
    config = HealConfig(
        langfuse=LangfuseConfig(host="fixture://langfuse", credential_source="fixture"),
        streams=[stream],
    )
    state.save_config(config)

    for index, run_id in enumerate(("a" * 32, "b" * 32, "c" * 32)):
        failure = MetricFailure(
            metric="step_count",
            kind="distributional",
            decision_rule="upper_prediction_bound",
            run_ids=[run_id],
            evidence_pointer=f".maida-heal/reports/{index}.json",
        )
        persist_failures(
            state,
            config,
            stream,
            [failure],
            source=FindingSource.SHADOW_WATCH,
            detected_at=NOW + timedelta(minutes=index),
        )
        state.reconcile_finding_events(config)
        EventJournal(tmp_path, config.events).flush()

    events = [
        json.loads(line)
        for line in (tmp_path / ".maida-heal" / "events.jsonl").read_text().splitlines()
    ]
    assert [event["type"] for event in events] == [
        "finding.opened",
        "finding.evidence_added",
        "finding.evidence_added",
    ]
    assert [event["data"]["run_ids"] for event in events] == [
        ["a" * 32],
        ["a" * 32, "b" * 32],
        ["a" * 32, "b" * 32, "c" * 32],
    ]
    assert len({event["event_id"] for event in events}) == 3
