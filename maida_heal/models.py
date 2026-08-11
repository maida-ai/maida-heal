"""Strict, versioned artifacts for the self-healing loop.

The models intentionally carry only structural evidence. Raw trace payloads have no
field in findings, closure reports, localization, or fixer attempts.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from maida_heal.constants import (
    DEFAULT_ALLOWED_PATH_PATTERNS,
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_DAILY_MERGE_BUDGET,
    DEFAULT_HOLDOUT_FRACTION,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_DIFF_LINES,
    DEFAULT_RECURRENCE_HOURS,
)


class StrictModel(BaseModel):
    """Base for user- and machine-written artifacts that must fail closed."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value


class FindingStatus(str, Enum):
    OPEN = "open"
    FIX_PROPOSED = "fix_proposed"
    VERIFYING = "verifying"
    CLOSED = "closed"
    FIX_REJECTED = "fix_rejected"
    EXPIRED = "expired"


class FindingSource(str, Enum):
    SHADOW_WATCH = "shadow_watch"
    DEMO = "demo"
    POST_MERGE_WATCH = "post_merge_watch"


class Actor(str, Enum):
    HUMAN = "human"
    FIXER = "fixer"
    VERIFIER = "verifier"
    SYSTEM = "system"


class LoopMode(str, Enum):
    """Configuration profiles ordered by the actions they authorize."""

    SHADOW = "shadow"
    PROPOSE = "propose"
    VERIFY = "verify"
    FULL = "full"

    @property
    def rank(self) -> int:
        return {
            LoopMode.SHADOW: 1,
            LoopMode.PROPOSE: 2,
            LoopMode.VERIFY: 3,
            LoopMode.FULL: 4,
        }[self]

    def allows(self, required: LoopMode) -> bool:
        return self.rank >= required.rank


class HistoryEvent(StrictModel):
    timestamp: datetime = Field(default_factory=utc_now)
    actor: Actor
    action: str = Field(min_length=1, max_length=120)
    detail: str = Field(min_length=1, max_length=2000)

    _aware = field_validator("timestamp")(_require_aware)


class MetricFailure(StrictModel):
    """A payload-free projection of one failed Maida aggregate result."""

    metric: str = Field(min_length=1, max_length=120)
    target: str | None = Field(default=None, min_length=1, max_length=200)
    kind: Literal["invariant", "measured", "distributional", "statistical"]
    verdict: Literal["fail"] = "fail"
    decision_rule: str = Field(min_length=1, max_length=120)
    run_ids: list[str] = Field(default_factory=list)
    structural_terms: list[str] = Field(default_factory=list, max_length=50)
    evidence_pointer: str = Field(min_length=1)
    observed: float | None = None
    prediction_bound: float | None = None
    harmful_exceedances: int | None = Field(default=None, ge=0)
    violations: int | None = Field(default=None, ge=0)

    @field_validator("run_ids")
    @classmethod
    def validate_run_ids(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[0-9a-f]{32}", value):
                raise ValueError("run IDs must be 32 lowercase hexadecimal characters")
        return list(dict.fromkeys(values))

    @field_validator("structural_terms")
    @classmethod
    def validate_structural_terms(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if 0 < len(value.strip()) <= 120]
        return list(dict.fromkeys(cleaned))


class LocalizationCandidate(StrictModel):
    path: str = Field(min_length=1)
    commit: str = Field(min_length=7, max_length=64)
    committed_at: datetime
    score: float = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)
    diff_excerpt: str = Field(default="", max_length=8000)

    _aware = field_validator("committed_at")(_require_aware)


class FixAttempt(StrictModel):
    number: int = Field(ge=1)
    fixer: Literal["claude-code", "api", "command"]
    branch: str = Field(min_length=1)
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    outcome: Literal[
        "running", "dry_run", "proposed", "rejected", "verified", "failed"
    ] = "running"
    changed_paths: list[str] = Field(default_factory=list)
    diff_lines: int = Field(default=0, ge=0)
    pull_request_number: int | None = Field(default=None, ge=1)
    pull_request_url: str | None = None

    _started_aware = field_validator("started_at")(_require_aware)

    @field_validator("finished_at")
    @classmethod
    def finished_must_be_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None


class MergeRecord(StrictModel):
    mode: Literal["human", "automatic"]
    merged_at: datetime
    commit: str = Field(min_length=7, max_length=64)
    pull_request_number: int = Field(ge=1)

    _aware = field_validator("merged_at")(_require_aware)


_TRANSITIONS: dict[FindingStatus, frozenset[FindingStatus]] = {
    FindingStatus.OPEN: frozenset({FindingStatus.FIX_PROPOSED, FindingStatus.EXPIRED}),
    FindingStatus.FIX_PROPOSED: frozenset(
        {FindingStatus.VERIFYING, FindingStatus.FIX_REJECTED, FindingStatus.EXPIRED}
    ),
    FindingStatus.VERIFYING: frozenset(
        {FindingStatus.CLOSED, FindingStatus.FIX_REJECTED, FindingStatus.EXPIRED}
    ),
    FindingStatus.FIX_REJECTED: frozenset(
        {FindingStatus.FIX_PROPOSED, FindingStatus.EXPIRED}
    ),
    FindingStatus.CLOSED: frozenset(),
    FindingStatus.EXPIRED: frozenset(),
}


class Finding(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    stream: str = Field(min_length=1, max_length=160)
    stream_id: str = Field(min_length=1, max_length=120)
    source: FindingSource
    status: FindingStatus = FindingStatus.OPEN
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(min_length=1, max_length=2000)
    detected_at: datetime = Field(default_factory=utc_now)
    onset_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    metric_failures: list[MetricFailure] = Field(min_length=1)
    localization: list[LocalizationCandidate] = Field(default_factory=list)
    attempts: list[FixAttempt] = Field(default_factory=list)
    cooldown_until: datetime | None = None
    closure_report: str | None = None
    merge: MergeRecord | None = None
    history: list[HistoryEvent] = Field(min_length=1)

    _detected_aware = field_validator("detected_at")(_require_aware)
    _updated_aware = field_validator("updated_at")(_require_aware)

    @field_validator("onset_at")
    @classmethod
    def onset_must_be_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None

    @field_validator("cooldown_until")
    @classmethod
    def cooldown_must_be_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None

    @model_validator(mode="after")
    def unique_metrics(self) -> Finding:
        metrics = [item.metric for item in self.metric_failures]
        if len(metrics) != len(set(metrics)):
            raise ValueError("metric_failures must contain unique metric names")
        return self

    @property
    def metric_names(self) -> tuple[str, ...]:
        return tuple(item.metric for item in self.metric_failures)

    def transition(
        self,
        target: FindingStatus,
        *,
        actor: Actor,
        action: str,
        detail: str,
        at: datetime | None = None,
    ) -> None:
        """Apply one validated lifecycle transition and append its audit event."""
        if target not in _TRANSITIONS[self.status]:
            raise ValueError(f"illegal finding transition: {self.status} -> {target}")
        timestamp = _require_aware(at or utc_now())
        self.status = target
        self.updated_at = timestamp
        self.history.append(
            HistoryEvent(
                timestamp=timestamp,
                actor=actor,
                action=action,
                detail=detail,
            )
        )


class ClosureCondition(StrictModel):
    name: Literal["specific_metrics", "holdouts", "no_new_failures"]
    passed: bool
    evidence_pointers: list[str] = Field(min_length=1)
    detail: str = Field(min_length=1, max_length=2000)


class ClosureReport(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    generated_at: datetime = Field(default_factory=utc_now)
    maida_report_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    verdict: Literal["closed", "rejected"]
    conditions: list[ClosureCondition] = Field(min_length=3, max_length=3)

    _aware = field_validator("generated_at")(_require_aware)

    @model_validator(mode="after")
    def complete_conditions(self) -> ClosureReport:
        names = {item.name for item in self.conditions}
        expected = {"specific_metrics", "holdouts", "no_new_failures"}
        if names != expected:
            raise ValueError("closure report must contain each required condition once")
        should_close = all(item.passed for item in self.conditions)
        if (self.verdict == "closed") != should_close:
            raise ValueError("closure verdict must match the three closure conditions")
        return self


class LangfuseConfig(StrictModel):
    host: str = Field(min_length=1)
    credential_source: Literal["environment", "config", "fixture"]
    metadata_keys: list[str] = Field(default_factory=list)
    window_days: int = Field(default=14, ge=1, le=90)
    cursor_overlap_seconds: int = Field(default=300, ge=0, le=3600)


class StreamSelector(StrictModel):
    grouping: Literal["session_pattern", "metadata", "trace_name"]
    grouping_key: str = Field(min_length=1)
    grouping_value_hash: str = Field(pattern=r"^[0-9a-f]{12}$")


EnvelopeMetric = Literal[
    "step_count", "tool_call_count", "latency_ms", "cost_tokens", "task_pass_rate"
]
EnvelopeMode = Literal["gating", "report_only", "disabled"]


class EnvelopeConfig(StrictModel):
    """Per-stream overrides for conservative generated policy envelopes."""

    coverage: float | None = Field(default=None, ge=0.5, lt=1)
    confidence: float = Field(default=0.95, ge=0.5, lt=1)
    metrics: dict[EnvelopeMetric, EnvelopeMode] = Field(default_factory=dict)


class StreamConfig(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    name: str = Field(min_length=1, max_length=160)
    grouping: Literal["session_pattern", "metadata", "trace_name"]
    grouping_key: str = Field(min_length=1)
    grouping_value_hash: str = Field(pattern=r"^[0-9a-f]{12}$")
    selectors: list[StreamSelector] = Field(default_factory=list)
    trace_names: list[str] = Field(min_length=1)
    enabled: bool = True
    mode: LoopMode | None = None
    envelope: EnvelopeConfig = Field(default_factory=EnvelopeConfig)
    outlier: bool = False
    status: Literal["watching", "insufficient-data", "excluded"] = "watching"

    @property
    def selected(self) -> bool:
        """Compatibility accessor for the pre-profile internal implementation."""
        return self.enabled

    @selected.setter
    def selected(self, value: bool) -> None:
        self.enabled = value


class FixesConfig(StrictModel):
    repo: str = Field(min_length=1)
    repo_local_path: str | None = Field(default=None, min_length=1)
    fixer: Literal["claude-code", "api", "command"]
    command: list[str] | None = None
    auto_propose: bool = True
    allowed_paths: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_PATH_PATTERNS)
    )
    max_attempts_per_finding: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=10)
    cooldown_hours: int = Field(default=DEFAULT_COOLDOWN_HOURS, ge=0, le=720)

    @model_validator(mode="after")
    def command_fixer_has_command(self) -> FixesConfig:
        if self.fixer == "command" and not self.command:
            raise ValueError("command fixer requires a nonempty command")
        return self

    def local_repo(self) -> Path:
        if self.repo_local_path is None:
            raise ValueError(
                "the config repository is not materialized; run "
                "`maida-heal config apply`"
            )
        return Path(self.repo_local_path).expanduser().resolve()


class GateConfig(StrictModel):
    enabled_at: datetime = Field(default_factory=utc_now)
    manifest_path: str = ".maida/heal.yaml"
    command: list[str] = Field(min_length=1)
    holdout_command: list[str] | None = None
    holdout_fraction: float = Field(default=DEFAULT_HOLDOUT_FRACTION, gt=0, lt=1)

    _aware = field_validator("enabled_at")(_require_aware)


class AutoMergeConfig(StrictModel):
    enabled_at: datetime = Field(default_factory=utc_now)
    max_diff_lines: int = Field(default=DEFAULT_MAX_DIFF_LINES, ge=1)
    daily_budget: int = Field(default=DEFAULT_DAILY_MERGE_BUDGET, ge=1)
    recurrence_hours: int = Field(default=DEFAULT_RECURRENCE_HOURS, ge=1)

    _aware = field_validator("enabled_at")(_require_aware)


class ActivationConfig(StrictModel):
    """Audit artifact recording who authorized unattended full mode."""

    acknowledged_by: str = Field(min_length=3, max_length=240)
    date: date
    statement: Literal["autonomous-fix-loop-authorized"]


class JsonlSinkConfig(StrictModel):
    kind: Literal["jsonl"] = "jsonl"
    path: str = Field(default=".maida-heal/events.jsonl", min_length=1)


class WebhookSinkConfig(StrictModel):
    kind: Literal["webhook"] = "webhook"
    url: str = Field(min_length=1)
    secret_env: str = Field(pattern=r"^[A-Z_][A-Z0-9_]*$")
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    max_attempts: int = Field(default=3, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=0.25, ge=0, le=30)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError(
                "webhook URL must be an http(s) URL without credentials or a fragment"
            )
        return value


class GitHubSinkConfig(StrictModel):
    kind: Literal["github"] = "github"
    enabled: bool = True


EventSinkConfig = Annotated[
    JsonlSinkConfig | WebhookSinkConfig | GitHubSinkConfig,
    Field(discriminator="kind"),
]


def _default_event_sinks() -> list[EventSinkConfig]:
    return [JsonlSinkConfig()]


class EventConfig(StrictModel):
    """Versioned event delivery configuration; local JSONL is always active."""

    sinks: list[EventSinkConfig] = Field(default_factory=_default_event_sinks)

    @model_validator(mode="after")
    def ensure_local_journal(self) -> EventConfig:
        if not any(isinstance(item, JsonlSinkConfig) for item in self.sinks):
            self.sinks.insert(0, JsonlSinkConfig())
        identities = [
            (
                item.kind,
                item.path if isinstance(item, JsonlSinkConfig) else None,
                item.url if isinstance(item, WebhookSinkConfig) else None,
            )
            for item in self.sinks
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("event sinks must be unique")
        return self


class EventType(str, Enum):
    FINDING_OPENED = "finding.opened"
    FINDING_EVIDENCE_ADDED = "finding.evidence_added"
    FIX_PROPOSED = "fix.proposed"
    FIX_REJECTED = "fix.rejected"
    FIX_VERIFIED = "fix.verified"
    FIX_EXPIRED = "fix.expired"
    LOOP_PAUSED = "loop.paused"
    LOOP_RESUMED = "loop.resumed"
    ROLLBACK_OPENED = "rollback.opened"


class FindingOpenedEventData(StrictModel):
    finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    stream: str = Field(min_length=1, max_length=160)
    stream_id: str = Field(min_length=1, max_length=120)
    source: FindingSource
    metrics: list[str] = Field(min_length=1)
    run_ids: list[str] = Field(default_factory=list)
    evidence_pointers: list[str] = Field(min_length=1)


class FindingEvidenceAddedEventData(StrictModel):
    finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    stream_id: str = Field(min_length=1, max_length=120)
    metrics: list[str] = Field(min_length=1)
    run_ids: list[str] = Field(default_factory=list)
    evidence_pointers: list[str] = Field(min_length=1)


class FixProposedEventData(StrictModel):
    finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    attempt: int = Field(ge=1)
    branch: str = Field(min_length=1)
    changed_paths: list[str] = Field(default_factory=list)
    diff_lines: int = Field(ge=0)
    pull_request_number: int | None = Field(default=None, ge=1)
    pull_request_url: str | None = None


class FixRejectedEventData(StrictModel):
    finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    attempt: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    changed_paths: list[str] = Field(default_factory=list)


class FixVerifiedEventData(StrictModel):
    finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    pull_request_number: int | None = Field(default=None, ge=1)
    release_mode: Literal["verify_only", "handoff", "auto_merge"]
    release_ready: bool
    closure_report: ClosureReport


class FixExpiredEventData(StrictModel):
    finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    reason: str = Field(min_length=1, max_length=2000)
    attempts: int = Field(ge=0)


class LoopStateEventData(StrictModel):
    actor: str = Field(min_length=1, max_length=240)
    reason: str | None = Field(default=None, max_length=1000)
    scope: Literal["all", "fix_dispatch"] = "all"


class RollbackOpenedEventData(StrictModel):
    finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    prior_finding_id: str = Field(pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    pull_request_url: str = Field(min_length=1)
    branch: str = Field(min_length=1)


EVENT_DATA_MODELS: dict[EventType, type[StrictModel]] = {
    EventType.FINDING_OPENED: FindingOpenedEventData,
    EventType.FINDING_EVIDENCE_ADDED: FindingEvidenceAddedEventData,
    EventType.FIX_PROPOSED: FixProposedEventData,
    EventType.FIX_REJECTED: FixRejectedEventData,
    EventType.FIX_VERIFIED: FixVerifiedEventData,
    EventType.FIX_EXPIRED: FixExpiredEventData,
    EventType.LOOP_PAUSED: LoopStateEventData,
    EventType.LOOP_RESUMED: LoopStateEventData,
    EventType.ROLLBACK_OPENED: RollbackOpenedEventData,
}

EventData = (
    FindingOpenedEventData
    | FindingEvidenceAddedEventData
    | FixProposedEventData
    | FixRejectedEventData
    | FixVerifiedEventData
    | FixExpiredEventData
    | LoopStateEventData
    | RollbackOpenedEventData
)


class EventEnvelope(StrictModel):
    """Stable machine-facing event contract with payload-free typed data."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    event_id: str = Field(pattern=r"^evt_[0-9a-f]{32}$")
    type: EventType
    occurred_at: datetime
    stream_id: str | None = Field(default=None, min_length=1, max_length=120)
    finding_id: str | None = Field(default=None, pattern=r"^mh-[0-9]{8}-[0-9a-f]{10}$")
    data: EventData

    _aware = field_validator("occurred_at")(_require_aware)

    @model_validator(mode="after")
    def validate_typed_data(self) -> EventEnvelope:
        expected = EVENT_DATA_MODELS[self.type]
        validated = expected.model_validate(self.data.model_dump(mode="json"))
        object.__setattr__(self, "data", validated)
        return self

    @classmethod
    def create(
        cls,
        *,
        event_type: EventType,
        occurred_at: datetime,
        dedupe_key: str,
        data: dict[str, Any],
        stream_id: str | None = None,
        finding_id: str | None = None,
    ) -> EventEnvelope:
        material = f"1.0.0\0{event_type.value}\0{dedupe_key}"
        identifier = "evt_" + hashlib.sha256(material.encode()).hexdigest()[:32]
        return cls(
            event_id=identifier,
            type=event_type,
            occurred_at=occurred_at,
            stream_id=stream_id,
            finding_id=finding_id,
            data=data,
        )


class HealConfig(StrictModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    mode: LoopMode = LoopMode.SHADOW
    langfuse: LangfuseConfig | None = None
    streams: list[StreamConfig] = Field(default_factory=list)
    fixes: FixesConfig | None = None
    gate: GateConfig | None = None
    auto_merge: AutoMergeConfig | None = None
    activation: ActivationConfig | None = None
    events: EventConfig = Field(default_factory=EventConfig)

    @model_validator(mode="after")
    def enforce_profile(self) -> HealConfig:
        if self.streams and self.langfuse is None:
            raise ValueError("streams require a Langfuse connection")
        if self.mode.allows(LoopMode.PROPOSE) and self.fixes is None:
            raise ValueError("propose mode requires fixes configuration")
        if self.mode.allows(LoopMode.VERIFY) and self.gate is None:
            raise ValueError("verify mode requires gate configuration")
        if self.mode is LoopMode.FULL and self.activation is None:
            raise ValueError(
                "full mode requires an activation attestation:\n"
                "activation:\n"
                '  acknowledged_by: "name/email"\n'
                '  date: "YYYY-MM-DD"\n'
                '  statement: "autonomous-fix-loop-authorized"'
            )
        if self.auto_merge is not None and self.mode is not LoopMode.FULL:
            raise ValueError("auto_merge requires full mode")
        for stream in self.streams:
            if stream.mode is not None and stream.mode.rank > self.mode.rank:
                raise ValueError(
                    f"stream {stream.id} mode cannot exceed global mode "
                    f"{self.mode.value}"
                )
        return self

    def effective_mode(self, stream: StreamConfig) -> LoopMode:
        return stream.mode or self.mode

    @property
    def release_mode(self) -> Literal["none", "handoff", "auto_merge"]:
        if self.mode is not LoopMode.FULL:
            return "none"
        return "auto_merge" if self.auto_merge is not None else "handoff"


class ImportRecord(StrictModel):
    source_trace_id: str = Field(min_length=1)
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    run_name: str = Field(min_length=1)
    stream_id: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    status: Literal["ok", "error"]
    duration_ms: float = Field(ge=0)
    llm_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)

    _started_aware = field_validator("started_at")(_require_aware)
    _ended_aware = field_validator("ended_at")(_require_aware)


class ImportIndex(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    records: list[ImportRecord] = Field(default_factory=list)
    last_window_start: datetime | None = None
    last_window_end: datetime | None = None
    stream_cursors: dict[str, datetime] = Field(default_factory=dict)

    @field_validator("last_window_start", "last_window_end")
    @classmethod
    def optional_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None

    @field_validator("stream_cursors")
    @classmethod
    def cursor_values_are_aware(
        cls, values: dict[str, datetime]
    ) -> dict[str, datetime]:
        return {key: _require_aware(value) for key, value in values.items()}

    def merge(self, records: list[ImportRecord]) -> None:
        by_id = {(item.stream_id, item.trace_id): item for item in self.records}
        by_id.update({(item.stream_id, item.trace_id): item for item in records})
        self.records = sorted(
            by_id.values(),
            key=lambda item: (item.started_at, item.stream_id, item.trace_id),
        )


class StreamHealth(StrictModel):
    stream_id: str = Field(min_length=1, max_length=120)
    status: Literal["healthy", "degraded", "never_run"] = "never_run"
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = Field(default=None, max_length=500)

    @field_validator("last_attempt_at", "last_success_at")
    @classmethod
    def health_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None


class RuntimeHealth(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    updated_at: datetime = Field(default_factory=utc_now)
    cycle_started_at: datetime | None = None
    cycle_completed_at: datetime | None = None
    streams: dict[str, StreamHealth] = Field(default_factory=dict)

    _updated_aware = field_validator("updated_at")(_require_aware)

    @field_validator("cycle_started_at", "cycle_completed_at")
    @classmethod
    def cycle_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None

    @property
    def status(self) -> Literal["healthy", "degraded", "unknown"]:
        if any(item.status == "degraded" for item in self.streams.values()):
            return "degraded"
        if self.streams and all(
            item.status == "healthy" for item in self.streams.values()
        ):
            return "healthy"
        return "unknown"


class StatusError(StrictModel):
    code: str | None = None
    message: str | None = None


class StatusStream(StrictModel):
    id: str
    name: str
    enabled: bool
    effective_mode: LoopMode
    health: Literal["healthy", "degraded", "never_run"]
    last_success_at: datetime | None = None
    error: StatusError | None = None

    @field_validator("last_success_at")
    @classmethod
    def status_timestamp_is_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None


class StatusAutonomy(StrictModel):
    detect: bool
    propose: bool
    verify: bool
    release: Literal["none", "handoff", "auto_merge"]


class StatusLimits(StrictModel):
    max_diff_lines: int = Field(ge=1)
    daily_merge_budget: int = Field(ge=1)
    recurrence_hours: int = Field(ge=1)


class StatusEventStream(StrictModel):
    format: Literal["jsonl"] = "jsonl"
    path: str = Field(min_length=1)
    pending: int = Field(ge=0)


class StatusReport(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    mode: LoopMode
    health: Literal["healthy", "degraded", "paused", "not_configured", "unknown"]
    paused: bool
    pause_scope: Literal["all", "fix_dispatch"] | None = None
    updated_at: datetime
    autonomy: StatusAutonomy
    limits: StatusLimits | None = None
    event_stream: StatusEventStream
    streams: list[StatusStream]

    _aware = field_validator("updated_at")(_require_aware)


class GateManifest(StrictModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    finding_schema_version: Literal["1.0.0"] = "1.0.0"
    closure_schema_version: Literal["1.0.0"] = "1.0.0"
    maida_requirement: Literal["maida-ai==0.5.0"] = "maida-ai==0.5.0"
    mode: LoopMode = LoopMode.VERIFY
    stream_modes: dict[str, LoopMode] = Field(default_factory=dict)
    command: list[str] = Field(min_length=1)
    holdout_command: list[str] | None = None
    holdout_fraction: float = Field(gt=0, lt=1)
    max_attempts_per_finding: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=10)
    cooldown_hours: int = Field(default=DEFAULT_COOLDOWN_HOURS, ge=0, le=720)
    events: EventConfig = Field(default_factory=EventConfig)
    activation: ActivationConfig | None = None
    auto_merge: AutoMergeConfig | None = None

    @model_validator(mode="after")
    def enforce_release_profile(self) -> GateManifest:
        if self.mode is LoopMode.FULL and self.activation is None:
            raise ValueError("full gate manifest requires activation attestation")
        if self.auto_merge is not None and self.mode is not LoopMode.FULL:
            raise ValueError("gate auto_merge requires full mode")
        if any(mode.rank > self.mode.rank for mode in self.stream_modes.values()):
            raise ValueError("gate stream mode cannot exceed manifest mode")
        return self


def jsonable(model: BaseModel, *, exclude_defaults: bool = False) -> dict[str, Any]:
    """Serialize an artifact using stable JSON-compatible values."""
    payload = model.model_dump(
        mode="json", exclude_none=True, exclude_defaults=exclude_defaults
    )
    if hasattr(model, "schema_version"):
        payload["schema_version"] = model.schema_version
    return payload
