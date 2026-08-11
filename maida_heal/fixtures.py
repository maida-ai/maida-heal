"""Deterministic public trace-schema fixtures for offline demos and walkthroughs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from maida_heal.langfuse import Observation


@dataclass(frozen=True)
class FixtureRun:
    source_trace_id: str
    trace_id: str
    run_name: str
    session_id: str
    started_at: datetime
    duration_ms: int
    tools: tuple[str, ...]
    total_tokens: int
    status: str = "ok"


def _hex_id(label: str, length: int) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:length]


def fixture_runs(*, regression: bool = True) -> list[FixtureRun]:
    """Return fourteen baseline traces and five recent candidate traces."""
    start = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    runs: list[FixtureRun] = []
    for index in range(19):
        is_recent = index >= 14
        is_regression = regression and is_recent
        tools = (
            ("lookup", "retry", "retry", "retry")
            if is_regression
            else ("lookup", "summarize")
            if index % 4 == 0
            else ("lookup",)
        )
        label = f"fixture-{'bad' if is_regression else 'good'}-{index}"
        runs.append(
            FixtureRun(
                source_trace_id=label,
                trace_id=_hex_id(label, 32),
                run_name="support-agent",
                session_id=f"support-session-{1000 + index}",
                started_at=start + timedelta(hours=index * 12),
                duration_ms=900 if is_regression else 150 + (index % 3) * 10,
                tools=tools,
                total_tokens=180 if is_regression else 25 + (index % 3),
            )
        )
    return runs


def fixed_candidate_runs(count: int = 5) -> list[FixtureRun]:
    """Return healthy post-patch candidates newer than the detection window."""
    if count < 1:
        raise ValueError("candidate fixture count must be positive")
    start = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
    return [
        FixtureRun(
            source_trace_id=f"fixture-fixed-{index}",
            trace_id=_hex_id(f"fixture-fixed-{index}", 32),
            run_name="support-agent",
            session_id=f"support-session-fixed-{index}",
            started_at=start + timedelta(hours=index),
            duration_ms=150 + (index % 3) * 10,
            tools=("lookup",),
            total_tokens=25 + (index % 3),
        )
        for index in range(count)
    ]


def fixture_observations(runs: list[FixtureRun]) -> list[Observation]:
    """Project fixture runs into documented Langfuse observation shapes."""
    observations: list[Observation] = []
    for run in runs:
        generation_end = run.started_at + timedelta(milliseconds=80)
        observations.append(
            Observation(
                id=f"{run.source_trace_id}-generation",
                trace_id=run.source_trace_id,
                trace_name=run.run_name,
                session_id=run.session_id,
                start_time=run.started_at,
                end_time=generation_end,
                observation_type="GENERATION",
                name="respond",
                metadata={"agent_id": "support-primary"},
                usage_total=float(run.total_tokens),
                total_cost=0.0,
            )
        )
        for tool_index, tool in enumerate(run.tools):
            tool_start = run.started_at + timedelta(milliseconds=90 + tool_index * 10)
            observations.append(
                Observation(
                    id=f"{run.source_trace_id}-tool-{tool_index}",
                    trace_id=run.source_trace_id,
                    trace_name=run.run_name,
                    session_id=run.session_id,
                    start_time=tool_start,
                    end_time=tool_start + timedelta(milliseconds=5),
                    observation_type="TOOL",
                    name=tool,
                    metadata={"agent_id": "support-primary"},
                    usage_total=0.0,
                    total_cost=0.0,
                )
            )
    return observations


def _timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _span(
    *,
    run: FixtureRun,
    span_id: str,
    parent_span_id: str | None,
    name: str,
    start: datetime,
    end: datetime,
    attributes: dict[str, object],
) -> dict[str, object]:
    return {
        "trace_id": run.trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "name": name,
        "kind": "INTERNAL",
        "start_time": _timestamp(start),
        "end_time": _timestamp(end),
        "duration_ms": int((end - start).total_seconds() * 1000),
        "attributes": attributes,
        "events": [],
        "status_code": "OK" if run.status == "ok" else "ERROR",
        "status_description": "",
    }


def materialize_run(run: FixtureRun, runs_dir: Path) -> Path:
    """Write one conforming native run using Maida's public trace schema 0.2."""
    destination = runs_dir / run.trace_id
    destination.mkdir(parents=True, exist_ok=True)
    ended_at = run.started_at + timedelta(milliseconds=run.duration_ms)
    meta = {
        "spec_version": "0.2",
        "trace_id": run.trace_id,
        "run_name": run.run_name,
        "started_at": _timestamp(run.started_at),
        "ended_at": _timestamp(ended_at),
        "duration_ms": run.duration_ms,
        "status": run.status,
        "counts": {
            "llm_calls": 1,
            "tool_calls": len(run.tools),
            "errors": 0 if run.status == "ok" else 1,
            "loop_warnings": 0,
        },
    }
    (destination / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    root_id = _hex_id(f"{run.trace_id}-root", 16)
    spans = [
        _span(
            run=run,
            span_id=root_id,
            parent_span_id=None,
            name=run.run_name,
            start=run.started_at,
            end=ended_at,
            attributes={"maida.run_name": run.run_name},
        )
    ]
    llm_start = run.started_at + timedelta(milliseconds=10)
    llm_end = run.started_at + timedelta(milliseconds=80)
    spans.append(
        _span(
            run=run,
            span_id=_hex_id(f"{run.trace_id}-llm", 16),
            parent_span_id=root_id,
            name="fixture-model",
            start=llm_start,
            end=llm_end,
            attributes={
                "gen_ai.system": "fixture",
                "gen_ai.usage.input_tokens": max(0, run.total_tokens - 10),
                "gen_ai.usage.output_tokens": min(10, run.total_tokens),
                "gen_ai.usage.total_tokens": run.total_tokens,
            },
        )
    )
    for index, tool in enumerate(run.tools):
        tool_start = run.started_at + timedelta(milliseconds=90 + index * 10)
        spans.append(
            _span(
                run=run,
                span_id=_hex_id(f"{run.trace_id}-tool-{index}", 16),
                parent_span_id=root_id,
                name=tool,
                start=tool_start,
                end=tool_start + timedelta(milliseconds=5),
                attributes={"maida.tool_name": tool, "maida.status": "ok"},
            )
        )
    (destination / "spans.jsonl").write_text(
        "".join(json.dumps(span, ensure_ascii=False) + "\n" for span in spans),
        encoding="utf-8",
    )
    return destination


def materialize_runs(runs: list[FixtureRun], data_dir: Path) -> dict[str, object]:
    """Install an idempotent fixture batch and return Maida import-summary shape."""
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    imported: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for run in runs:
        destination = runs_dir / run.trace_id
        item = {
            "source_trace_id": run.source_trace_id,
            "trace_id": run.trace_id,
            "run_name": run.run_name,
        }
        if destination.exists():
            skipped.append({**item, "reason": "already imported"})
        else:
            materialize_run(run, runs_dir)
            imported.append(item)
    return {"imported": imported, "skipped": skipped, "unmapped_observation_types": []}
