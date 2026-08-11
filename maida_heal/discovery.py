"""Infer logical agent streams from heterogeneous Langfuse trace populations."""

from __future__ import annotations

import hashlib
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from maida_heal.langfuse import Observation


def stable_hash(value: str, *, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "stream")[:56].rstrip("-")


def session_pattern(value: str) -> str | None:
    """Return a reusable session pattern, or None for a one-off opaque value."""
    normalized = re.sub(
        r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        "<id>",
        value,
    )
    normalized = re.sub(
        r"(?i)(?<![a-z0-9])[0-9a-f]{12,}(?![a-z0-9])", "<id>", normalized
    )
    normalized = re.sub(r"(?<![a-z0-9])[0-9]{3,}(?![a-z0-9])", "<n>", normalized)
    return normalized if normalized != value else None


@dataclass(frozen=True)
class TraceSummary:
    trace_id: str
    trace_name: str
    session_id: str | None
    metadata: dict[str, str]
    started_at: datetime
    ended_at: datetime
    duration_ms: float
    observation_count: int
    tool_calls: int
    llm_calls: int
    usage_total: float
    total_cost: float


@dataclass
class StreamCandidate:
    id: str
    name: str
    grouping: str
    grouping_key: str
    grouping_value: str
    grouping_value_hash: str
    selectors: list[tuple[str, str, str]]
    trace_ids: list[str]
    trace_names: list[str]
    trace_count: int
    outlier: bool
    selected: bool


def summarize_traces(observations: Iterable[Observation]) -> list[TraceSummary]:
    groups: dict[str, list[Observation]] = defaultdict(list)
    for item in observations:
        groups[item.trace_id].append(item)
    summaries: list[TraceSummary] = []
    for trace_id, rows in groups.items():
        rows.sort(key=lambda item: (item.start_time, item.id))
        first = rows[0]
        end_times = [item.end_time or item.start_time for item in rows]
        started_at = min(item.start_time for item in rows)
        ended_at = max(end_times)
        metadata: dict[str, str] = {}
        for row in rows:
            metadata.update(row.metadata)
        summaries.append(
            TraceSummary(
                trace_id=trace_id,
                trace_name=first.trace_name,
                session_id=next(
                    (row.session_id for row in rows if row.session_id), None
                ),
                metadata=metadata,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=max(0.0, (ended_at - started_at).total_seconds() * 1000),
                observation_count=len(rows),
                tool_calls=sum(row.observation_type.upper() == "TOOL" for row in rows),
                llm_calls=sum(
                    row.observation_type.upper() == "GENERATION" for row in rows
                ),
                usage_total=sum(row.usage_total for row in rows),
                total_cost=sum(row.total_cost for row in rows),
            )
        )
    return sorted(summaries, key=lambda item: (item.started_at, item.trace_id))


def _grouping_for_trace(
    trace: TraceSummary,
    *,
    reusable_sessions: frozenset[str],
    metadata_keys: tuple[str, ...],
    reusable_metadata: frozenset[tuple[str, str]],
) -> tuple[str, str, str]:
    if trace.session_id:
        pattern = session_pattern(trace.session_id)
        if pattern and pattern in reusable_sessions:
            return "session_pattern", "sessionId", pattern
    for key in metadata_keys:
        value = trace.metadata.get(key)
        if value is not None and (key, value) in reusable_metadata:
            return "metadata", key, value
    return "trace_name", "traceName", trace.trace_name


def discover_streams(
    observations: Iterable[Observation],
    *,
    metadata_keys: Iterable[str],
) -> list[StreamCandidate]:
    traces = summarize_traces(observations)
    selected_keys = tuple(dict.fromkeys(metadata_keys))

    session_counts = Counter(
        pattern
        for trace in traces
        if trace.session_id and (pattern := session_pattern(trace.session_id))
    )
    reusable_sessions = frozenset(
        pattern for pattern, count in session_counts.items() if count >= 2
    )
    metadata_counts = Counter(
        (key, trace.metadata[key])
        for trace in traces
        for key in selected_keys
        if key in trace.metadata
    )
    reusable_metadata = frozenset(
        pair for pair, count in metadata_counts.items() if count >= 2
    )

    grouped: dict[tuple[str, str, str], list[TraceSummary]] = defaultdict(list)
    for trace in traces:
        grouping = _grouping_for_trace(
            trace,
            reusable_sessions=reusable_sessions,
            metadata_keys=selected_keys,
            reusable_metadata=reusable_metadata,
        )
        grouped[grouping].append(trace)

    candidates: list[StreamCandidate] = []
    for (kind, key, value), items in sorted(grouped.items()):
        names = sorted({item.trace_name for item in items})
        durations = [item.duration_ms for item in items]
        median_duration = statistics.median(durations) if durations else 0.0
        high_variance = bool(durations) and max(durations) > max(
            600_000.0, median_duration * 10
        )
        interactive_shape = median_duration > 300_000 or (
            statistics.median([item.observation_count for item in items]) > 100
        )
        expensive_shape = (
            statistics.median([item.usage_total for item in items]) > 100_000
            or statistics.median([item.total_cost for item in items]) > 1.0
        )
        outlier = high_variance or interactive_shape or expensive_shape
        digest = stable_hash(f"{kind}\0{key}\0{value}")
        base_name = names[0] if len(names) == 1 else " + ".join(names[:2])
        if kind == "session_pattern":
            name = f"{base_name} / session:{digest[:6]}"
        elif kind == "metadata":
            name = f"{base_name} / {key}:{digest[:6]}"
        else:
            name = base_name
        candidates.append(
            StreamCandidate(
                id=f"{_slug(base_name)}-{digest[:8]}",
                name=name,
                grouping=kind,
                grouping_key=key,
                grouping_value=value,
                grouping_value_hash=digest,
                selectors=[(kind, key, value)],
                trace_ids=[item.trace_id for item in items],
                trace_names=names,
                trace_count=len(items),
                outlier=outlier,
                selected=not outlier,
            )
        )
    return candidates


def match_stream(
    trace: TraceSummary,
    *,
    grouping: str,
    grouping_key: str,
    grouping_value_hash: str,
) -> bool:
    if grouping == "session_pattern":
        value = session_pattern(trace.session_id or "")
    elif grouping == "metadata":
        value = trace.metadata.get(grouping_key)
    else:
        value = trace.trace_name
    if value is None:
        return False
    return stable_hash(f"{grouping}\0{grouping_key}\0{value}") == grouping_value_hash
