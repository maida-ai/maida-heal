"""Headless attachment and foreground watch orchestration."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from maida.heal.artifacts import (
    TargetArtifacts,
    prepare_stream_artifacts,
    rebuild_window,
    safe_name,
)
from maida.heal.core import MaidaCLI
from maida.heal.detection import DetectionResult, evaluate_stream
from maida.heal.discovery import (
    StreamCandidate,
    TraceSummary,
    discover_streams,
    match_stream,
    stable_hash,
    summarize_traces,
)
from maida.heal.fixtures import FixtureRun, fixture_runs, materialize_runs
from maida.heal.langfuse import FixtureClient, LangfuseClient, Observation
from maida.heal.models import (
    HealConfig,
    ImportIndex,
    ImportRecord,
    LangfuseConfig,
    LoopMode,
    RuntimeHealth,
    StreamConfig,
    StreamHealth,
    StreamSelector,
)
from maida.heal.state import StateStore, read_json

Progress = Callable[[str], None]


@dataclass(frozen=True)
class AttachResult:
    streams: tuple[StreamConfig, ...]
    traces: int
    window_start: datetime
    window_end: datetime
    detections: tuple[DetectionResult, ...]
    errors: tuple[StreamFailure, ...] = ()


@dataclass(frozen=True)
class StreamFailure:
    stream_id: str
    phase: str
    error_code: str
    message: str = "stream cycle failed; inspect local structural reports"


@dataclass(frozen=True)
class AttachPlan:
    streams: tuple[StreamConfig, ...]
    traces: int
    window_start: datetime
    window_end: datetime
    writes: tuple[str, ...]


def candidate_to_config(candidate: StreamCandidate) -> StreamConfig:
    return StreamConfig(
        id=candidate.id,
        name=candidate.name,
        grouping=candidate.grouping,
        grouping_key=candidate.grouping_key,
        grouping_value_hash=candidate.grouping_value_hash,
        selectors=[
            StreamSelector(
                grouping=grouping,
                grouping_key=key,
                grouping_value_hash=stable_hash(f"{grouping}\0{key}\0{value}"),
            )
            for grouping, key, value in candidate.selectors
        ],
        trace_names=candidate.trace_names,
        enabled=candidate.selected,
        outlier=candidate.outlier,
        status="excluded" if not candidate.selected else "watching",
    )


def plan_attachment(
    state: StateStore,
    client: LangfuseClient,
    *,
    now: datetime,
    metadata_keys: list[str],
) -> AttachPlan:
    """Discover the exact headless defaults without writing local state."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("attachment clock must include a timezone")
    start = now - timedelta(days=14)
    client.validate()
    observations = client.discover(
        from_time=start, to_time=now, metadata_keys=metadata_keys
    )
    if not observations:
        raise ValueError("No Langfuse traces were found in the selected window")
    streams = tuple(
        candidate_to_config(item)
        for item in discover_streams(observations, metadata_keys=metadata_keys)
    )
    writes = [state.config_path.relative_to(state.project_root).as_posix()]
    for stream in streams:
        if stream.enabled:
            writes.append(
                (state.streams_dir / stream.id / "<target>" / "policy.yaml")
                .relative_to(state.project_root)
                .as_posix()
            )
    writes.extend(
        [
            state.imports_path.relative_to(state.project_root).as_posix(),
            ".maida-heal/findings/<finding-id>.json",
            ".maida-heal/events.jsonl",
        ]
    )
    return AttachPlan(
        streams=streams,
        traces=len(summarize_traces(observations)),
        window_start=start,
        window_end=now,
        writes=tuple(dict.fromkeys(writes)),
    )


def apply_stream_edits(
    candidates: list[StreamCandidate],
    *,
    select_all: bool,
    selected_ids: set[str] | None = None,
    excluded_ids: set[str] | None = None,
    renames: dict[str, str] | None = None,
    merges: dict[str, list[str]] | None = None,
) -> list[StreamCandidate]:
    """Apply deterministic config/fixture stream edits to inference results."""
    by_id = {item.id: item for item in candidates}
    if select_all:
        for item in candidates:
            item.selected = True
    elif selected_ids is not None:
        unknown = selected_ids - by_id.keys()
        if unknown:
            raise ValueError(f"unknown stream selection: {', '.join(sorted(unknown))}")
        for item in candidates:
            item.selected = item.id in selected_ids
    for identifier in excluded_ids or set():
        if identifier not in by_id:
            raise ValueError(f"unknown stream exclusion: {identifier}")
        by_id[identifier].selected = False
    for identifier, name in (renames or {}).items():
        if identifier not in by_id:
            raise ValueError(f"unknown stream rename: {identifier}")
        if not name.strip():
            raise ValueError("stream names must not be empty")
        by_id[identifier].name = name.strip()
    consumed: set[str] = set()
    for target_id, source_ids in (merges or {}).items():
        if target_id not in by_id:
            raise ValueError(f"unknown merge target: {target_id}")
        target = by_id[target_id]
        for source_id in source_ids:
            if source_id == target_id:
                continue
            if source_id not in by_id:
                raise ValueError(f"unknown merge source: {source_id}")
            source = by_id[source_id]
            target.trace_ids = list(
                dict.fromkeys([*target.trace_ids, *source.trace_ids])
            )
            target.trace_names = sorted({*target.trace_names, *source.trace_names})
            target.selectors = list(
                dict.fromkeys([*target.selectors, *source.selectors])
            )
            target.trace_count += source.trace_count
            target.outlier = target.outlier or source.outlier
            target.selected = target.selected or source.selected
            consumed.add(source_id)
    return [item for item in candidates if item.id not in consumed]


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Maida run has no {field}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Maida run {field} must include a timezone")
    return parsed


def _numeric(value: object, default: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _summary_map(payload: dict[str, object]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key in ("imported", "skipped"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            source = item.get("source_trace_id")
            trace = item.get("trace_id")
            if isinstance(source, str) and isinstance(trace, str):
                mapping[source] = trace
    return mapping


def _stream_for_trace(
    trace: TraceSummary, streams: Iterable[StreamConfig]
) -> StreamConfig | None:
    for stream in streams:
        selectors = stream.selectors or [
            StreamSelector(
                grouping=stream.grouping,
                grouping_key=stream.grouping_key,
                grouping_value_hash=stream.grouping_value_hash,
            )
        ]
        if any(
            match_stream(
                trace,
                grouping=selector.grouping,
                grouping_key=selector.grouping_key,
                grouping_value_hash=selector.grouping_value_hash,
            )
            for selector in selectors
        ):
            return stream
    return None


def build_import_records(
    observations: list[Observation],
    import_summary: dict[str, object],
    maida_runs: list[dict[str, object]],
    streams: list[StreamConfig],
) -> list[ImportRecord]:
    source_to_trace = _summary_map(import_summary)
    listed = {
        item.get("trace_id") or item.get("run_id"): item
        for item in maida_runs
        if isinstance(item.get("trace_id") or item.get("run_id"), str)
    }
    records: list[ImportRecord] = []
    for trace in summarize_traces(observations):
        imported_id = source_to_trace.get(trace.trace_id)
        if imported_id is None:
            continue
        run = listed.get(imported_id)
        if run is None:
            continue
        stream = _stream_for_trace(trace, streams)
        if stream is None or not stream.selected:
            continue
        counts = run.get("counts")
        counts = counts if isinstance(counts, dict) else {}
        started = (
            _parse_time(run.get("started_at"), field="started_at")
            if run.get("started_at") is not None
            else trace.started_at
        )
        ended = (
            _parse_time(run.get("ended_at"), field="ended_at")
            if run.get("ended_at") is not None
            else trace.ended_at
        )
        status = run.get("status")
        if status not in {"ok", "error"}:
            continue
        records.append(
            ImportRecord(
                source_trace_id=trace.trace_id,
                trace_id=imported_id,
                run_name=(
                    str(run["run_name"])
                    if isinstance(run.get("run_name"), str) and run["run_name"]
                    else trace.trace_name
                ),
                stream_id=stream.id,
                started_at=started,
                ended_at=ended,
                status=status,
                duration_ms=_numeric(run.get("duration_ms"), trace.duration_ms),
                llm_calls=int(counts.get("llm_calls") or trace.llm_calls),
                tool_calls=int(counts.get("tool_calls") or trace.tool_calls),
            )
        )
    return records


def _load_targets(
    state: StateStore,
    stream: StreamConfig,
    records: list[ImportRecord],
) -> list[TargetArtifacts]:
    targets: list[TargetArtifacts] = []
    stream_dir = state.streams_dir / stream.id
    if not stream_dir.exists():
        return []
    by_id = {item.trace_id: item for item in records}
    for metadata_path in sorted(stream_dir.glob("*/metadata.json")):
        payload = read_json(metadata_path)
        run_name = payload.get("run_name")
        baseline_ids = payload.get("baseline_run_ids")
        if not isinstance(run_name, str) or not isinstance(baseline_ids, list):
            continue
        baseline_set = {item for item in baseline_ids if isinstance(item, str)}
        recent = [
            item
            for item in records
            if item.run_name == run_name and item.trace_id not in baseline_set
        ]
        if not recent:
            continue
        recent_window = rebuild_window(
            state.windows_dir / stream.id / safe_name(run_name) / "recent",
            recent,
            state.runs_dir,
        )
        root = metadata_path.parent
        if (
            not (root / "baseline.json").is_file()
            or not (root / "policy.yaml").is_file()
        ):
            continue
        targets.append(
            TargetArtifacts(
                stream_id=stream.id,
                run_name=run_name,
                baseline=root / "baseline.json",
                policy=root / "policy.yaml",
                recent_window=recent_window,
                baseline_count=sum(item in by_id for item in baseline_set),
                recent_count=len(recent),
            )
        )
    return targets


def _import_batch(
    state: StateStore,
    core: MaidaCLI,
    *,
    start: datetime,
    end: datetime,
    host: str,
    fixture_batch: list[FixtureRun] | None,
) -> dict[str, object]:
    if fixture_batch is not None:
        return materialize_runs(fixture_batch, state.data_dir)
    return core.import_langfuse(from_time=start, to_time=end, host=host)


def attach(
    state: StateStore,
    core: MaidaCLI,
    client: LangfuseClient,
    *,
    host: str,
    credential_source: str,
    now: datetime,
    metadata_keys: list[str],
    configure: Callable[[list[StreamCandidate]], list[StreamCandidate]] | None,
    progress: Progress,
    fixture_batch: list[FixtureRun] | None = None,
) -> AttachResult:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("attachment clock must include a timezone")
    start = now - timedelta(days=14)
    progress("Connect — validating read-only Langfuse access")
    client.validate()
    progress("Discover — reading structural fields from an absolute 14-day window")
    observations = client.discover(
        from_time=start, to_time=now, metadata_keys=metadata_keys
    )
    if not observations:
        raise ValueError("No Langfuse traces were found in the selected window")
    discovered = discover_streams(observations, metadata_keys=metadata_keys)
    candidates = configure(discovered) if configure is not None else discovered
    streams = [candidate_to_config(item) for item in candidates]
    if not any(item.selected for item in streams):
        raise ValueError("At least one discovered stream must be selected")

    state.initialize()
    progress("Import — running `maida import langfuse` with absolute bounds")
    imported = _import_batch(
        state,
        core,
        start=start,
        end=now,
        host=host,
        fixture_batch=fixture_batch,
    )
    run_rows = core.list_runs()
    records = build_import_records(observations, imported, run_rows, streams)
    index = ImportIndex(last_window_start=start, last_window_end=now)
    index.merge(records)
    state.save_imports(index)
    config = HealConfig(
        mode=LoopMode.SHADOW,
        langfuse=LangfuseConfig(
            host=host,
            credential_source=credential_source,
            metadata_keys=metadata_keys,
        ),
        streams=streams,
    )
    state.save_config(config)

    progress("Baseline — deriving conservative policies from older samples")
    detections: list[DetectionResult] = []
    errors: list[StreamFailure] = []
    health = RuntimeHealth(
        updated_at=now,
        cycle_started_at=now,
        streams={
            stream.id: StreamHealth(stream_id=stream.id)
            for stream in streams
            if stream.enabled
        },
    )
    for stream in streams:
        if not stream.enabled:
            continue
        health.streams[stream.id].last_attempt_at = now
        try:
            stream_records = [item for item in records if item.stream_id == stream.id]
            targets = prepare_stream_artifacts(
                state, core, stream, stream_records, generated_at=now
            )
            if not targets:
                stream.status = "insufficient-data"
            else:
                progress(f"Report — comparing the recent slice for {stream.name}")
                detections.append(
                    evaluate_stream(
                        state, core, config, stream, targets, detected_at=now
                    )
                )
            health.streams[stream.id].status = "healthy"
            health.streams[stream.id].last_success_at = now
        except Exception as error:
            stream.status = "watching"
            health.streams[stream.id].status = "degraded"
            health.streams[stream.id].error_code = type(error).__name__
            health.streams[
                stream.id
            ].error_message = (
                "stream attachment failed; inspect local structural reports"
            )
            errors.append(
                StreamFailure(stream.id, "baseline_compare", type(error).__name__)
            )
    state.save_config(config)
    health.updated_at = now
    health.cycle_completed_at = now
    state.save_health(health)
    return AttachResult(
        streams=tuple(streams),
        traces=len(records),
        window_start=start,
        window_end=now,
        detections=tuple(detections),
        errors=tuple(errors),
    )


def watch_once(
    state: StateStore,
    core: MaidaCLI,
    client: LangfuseClient,
    *,
    now: datetime,
    progress: Progress,
    fixture_batch: list[FixtureRun] | None = None,
    checkpoint: Callable[[str], None] | None = None,
) -> AttachResult:
    config = state.load_config()
    if config.langfuse is None:
        raise ValueError("shadow mode is not configured; run `maida-heal up`")
    index = state.load_imports()
    floor = now - timedelta(days=config.langfuse.window_days)
    enabled_streams = [item for item in config.streams if item.enabled]
    cursors = [
        index.stream_cursors.get(stream.id, index.last_window_end or floor)
        for stream in enabled_streams
    ]
    cursor = min(cursors, default=index.last_window_end or floor)
    start = max(
        floor,
        cursor - timedelta(seconds=config.langfuse.cursor_overlap_seconds),
    )
    if start >= now:
        start = max(floor, now - timedelta(seconds=1))
    progress(f"Import — checking {start.isoformat()} to {now.isoformat()}")
    client.validate()
    observations = client.discover(
        from_time=start,
        to_time=now,
        metadata_keys=config.langfuse.metadata_keys,
    )
    imported = _import_batch(
        state,
        core,
        start=start,
        end=now,
        host=config.langfuse.host,
        fixture_batch=fixture_batch,
    )
    run_rows = core.list_runs()
    errors: list[StreamFailure] = []
    health = state.load_health()
    health.cycle_started_at = now
    health.cycle_completed_at = None
    health.updated_at = now
    for stream in enabled_streams:
        health.streams.setdefault(stream.id, StreamHealth(stream_id=stream.id))
        health.streams[stream.id].last_attempt_at = now
        try:
            new_records = build_import_records(
                observations, imported, run_rows, [stream]
            )
            index.merge(new_records)
            index.stream_cursors[stream.id] = now
        except Exception as error:
            health.streams[stream.id].status = "degraded"
            health.streams[stream.id].error_code = type(error).__name__
            health.streams[
                stream.id
            ].error_message = "stream import failed; inspect local structural reports"
            errors.append(StreamFailure(stream.id, "import", type(error).__name__))
    index.last_window_start = start
    index.last_window_end = now
    state.save_imports(index)
    if checkpoint is not None:
        checkpoint("imports_persisted")
    detections: list[DetectionResult] = []
    failed_imports = {item.stream_id for item in errors if item.phase == "import"}
    for stream in config.streams:
        if not stream.enabled or stream.id in failed_imports:
            continue
        try:
            records = [item for item in index.records if item.stream_id == stream.id]
            targets = _load_targets(state, stream, records)
            if not targets:
                targets = prepare_stream_artifacts(
                    state, core, stream, records, generated_at=now
                )
            if not targets:
                stream.status = "insufficient-data"
            else:
                stream.status = "watching"
                detections.append(
                    evaluate_stream(
                        state, core, config, stream, targets, detected_at=now
                    )
                )
            health.streams[stream.id].status = "healthy"
            health.streams[stream.id].last_success_at = now
            health.streams[stream.id].error_code = None
            health.streams[stream.id].error_message = None
        except Exception as error:
            health.streams[stream.id].status = "degraded"
            health.streams[stream.id].error_code = type(error).__name__
            health.streams[
                stream.id
            ].error_message = (
                "stream comparison failed; inspect local structural reports"
            )
            errors.append(StreamFailure(stream.id, "compare", type(error).__name__))
        if checkpoint is not None:
            checkpoint(f"stream_complete:{stream.id}")
    state.save_config(config)
    health.updated_at = now
    health.cycle_completed_at = now
    state.save_health(health)
    return AttachResult(
        streams=tuple(config.streams),
        traces=len(index.records),
        window_start=start,
        window_end=now,
        detections=tuple(detections),
        errors=tuple(errors),
    )


def purge_imported_data(state: StateStore) -> int:
    """Remove imported native runs and derived windows; keep structural findings."""
    removed = 0
    for path in (state.data_dir, state.windows_dir):
        if path.exists() and path.is_relative_to(state.root):
            removed += sum(1 for item in path.rglob("*") if item.is_file())
            shutil.rmtree(path)
    return removed


def fixture_attachment_client() -> tuple[list[FixtureRun], FixtureClient]:
    batch = fixture_runs(regression=True)
    from maida.heal.fixtures import fixture_observations

    return batch, FixtureClient(fixture_observations(batch))
