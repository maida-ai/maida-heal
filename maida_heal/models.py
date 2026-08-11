"""Strict, versioned artifacts for the self-healing loop.

The models intentionally carry only structural evidence. Raw trace payloads have no
field in findings, closure reports, localization, or fixer attempts.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

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
    credential_source: Literal["environment", "config", "prompt", "fixture"]
    metadata_keys: list[str] = Field(default_factory=list)
    window_days: int = Field(default=14, ge=1, le=90)


class StreamSelector(StrictModel):
    grouping: Literal["session_pattern", "metadata", "trace_name"]
    grouping_key: str = Field(min_length=1)
    grouping_value_hash: str = Field(pattern=r"^[0-9a-f]{12}$")


class StreamConfig(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
    name: str = Field(min_length=1, max_length=160)
    grouping: Literal["session_pattern", "metadata", "trace_name"]
    grouping_key: str = Field(min_length=1)
    grouping_value_hash: str = Field(pattern=r"^[0-9a-f]{12}$")
    selectors: list[StreamSelector] = Field(default_factory=list)
    trace_names: list[str] = Field(min_length=1)
    selected: bool = True
    outlier: bool = False
    status: Literal["watching", "insufficient-data", "excluded"] = "watching"


class FixesConfig(StrictModel):
    repo: str = Field(min_length=1)
    repo_local_path: str = Field(min_length=1)
    fixer: Literal["claude-code", "api", "command"]
    command: list[str] | None = None
    auto_propose: bool = False
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


class HealConfig(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    langfuse: LangfuseConfig | None = None
    streams: list[StreamConfig] = Field(default_factory=list)
    fixes: FixesConfig | None = None
    gate: GateConfig | None = None
    auto_merge: AutoMergeConfig | None = None

    @model_validator(mode="after")
    def enforce_tier_order(self) -> HealConfig:
        if self.streams and self.langfuse is None:
            raise ValueError("streams require a Langfuse connection")
        if self.gate is not None and self.fixes is None:
            raise ValueError("gate requires fixes to be enabled")
        if self.auto_merge is not None and self.gate is None:
            raise ValueError("auto_merge requires the gate to be enabled")
        return self

    @property
    def tier(self) -> int:
        if self.auto_merge is not None:
            return 4
        if self.gate is not None:
            return 3
        if self.fixes is not None:
            return 2
        if self.langfuse is not None:
            return 1
        return 0


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

    @field_validator("last_window_start", "last_window_end")
    @classmethod
    def optional_aware(cls, value: datetime | None) -> datetime | None:
        return _require_aware(value) if value is not None else None

    def merge(self, records: list[ImportRecord]) -> None:
        by_id = {item.trace_id: item for item in self.records}
        by_id.update({item.trace_id: item for item in records})
        self.records = sorted(
            by_id.values(), key=lambda item: (item.started_at, item.trace_id)
        )


class GateManifest(StrictModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    finding_schema_version: Literal["1.0.0"] = "1.0.0"
    closure_schema_version: Literal["1.0.0"] = "1.0.0"
    maida_requirement: Literal["maida-ai==0.5.0"] = "maida-ai==0.5.0"
    command: list[str] = Field(min_length=1)
    holdout_command: list[str] | None = None
    holdout_fraction: float = Field(gt=0, lt=1)
    max_attempts_per_finding: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=10)
    cooldown_hours: int = Field(default=DEFAULT_COOLDOWN_HOURS, ge=0, le=720)
    auto_merge: AutoMergeConfig | None = None


def jsonable(model: BaseModel, *, exclude_defaults: bool = False) -> dict[str, Any]:
    """Serialize an artifact using stable JSON-compatible values."""
    payload = model.model_dump(
        mode="json", exclude_none=True, exclude_defaults=exclude_defaults
    )
    if hasattr(model, "schema_version"):
        payload["schema_version"] = model.schema_version
    return payload
