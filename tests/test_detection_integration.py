from datetime import datetime, timezone
from pathlib import Path

from maida_heal.artifacts import prepare_stream_artifacts
from maida_heal.core import MaidaCLI
from maida_heal.detection import evaluate_stream
from maida_heal.fixtures import fixture_runs, materialize_runs
from maida_heal.models import (
    HealConfig,
    ImportRecord,
    LangfuseConfig,
    StreamConfig,
)
from maida_heal.state import StateStore

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def records(regression: bool) -> list[ImportRecord]:
    return [
        ImportRecord(
            source_trace_id=item.source_trace_id,
            trace_id=item.trace_id,
            run_name=item.run_name,
            stream_id="support-agent-aabbccdd",
            started_at=item.started_at,
            ended_at=item.started_at,
            status="ok",
            duration_ms=item.duration_ms,
            llm_calls=1,
            tool_calls=len(item.tools),
        )
        for item in fixture_runs(regression=regression)
    ]


def stream() -> StreamConfig:
    return StreamConfig(
        id="support-agent-aabbccdd",
        name="support-agent / support-session-<n>",
        grouping="session_pattern",
        grouping_key="sessionId",
        grouping_value_hash="aabbccddeeff",
        trace_names=["support-agent"],
    )


def config(item: StreamConfig) -> HealConfig:
    return HealConfig(
        langfuse=LangfuseConfig(host="fixture://langfuse", credential_source="fixture"),
        streams=[item],
    )


def test_real_maida_drift_creates_and_deduplicates_step_regression(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path)
    state.initialize()
    materialize_runs(fixture_runs(regression=True), state.data_dir)
    selected = stream()
    selected_config = config(selected)
    state.save_config(selected_config)
    core = MaidaCLI(state)

    targets = prepare_stream_artifacts(
        state, core, selected, records(True), generated_at=NOW
    )
    result = evaluate_stream(
        state, core, selected_config, selected, targets, detected_at=NOW
    )

    assert result.verdict == "fail"
    assert result.findings_created
    findings = state.list_findings(selected_config)
    assert {finding.metric_names[0] for finding in findings} >= {
        "step_count",
        "tool_call_count",
    }
    policy = targets[0].policy.read_text(encoding="utf-8")
    assert "kind: distributional" in policy
    assert "generated: true" in policy
    assert "Suggested invariants (not enforced)" in policy

    repeated = evaluate_stream(
        state, core, selected_config, selected, targets, detected_at=NOW
    )
    assert repeated.findings_created == ()
    assert repeated.findings_updated
    assert len(state.list_findings(selected_config)) == len(findings)


def test_harmless_fixture_variance_produces_no_findings(tmp_path: Path) -> None:
    state = StateStore(tmp_path)
    state.initialize()
    materialize_runs(fixture_runs(regression=False), state.data_dir)
    selected = stream()
    selected_config = config(selected)
    state.save_config(selected_config)
    core = MaidaCLI(state)

    targets = prepare_stream_artifacts(
        state, core, selected, records(False), generated_at=NOW
    )
    result = evaluate_stream(
        state, core, selected_config, selected, targets, detected_at=NOW
    )

    assert result.verdict in {"pass", "inconclusive"}
    assert state.list_findings(selected_config) == []


def test_insufficient_sample_refuses_to_generate_policy(tmp_path: Path) -> None:
    state = StateStore(tmp_path)
    state.initialize()
    short = fixture_runs(regression=False)[:9]
    materialize_runs(short, state.data_dir)

    targets = prepare_stream_artifacts(
        state,
        MaidaCLI(state),
        stream(),
        records(False)[:9],
        generated_at=NOW,
    )

    assert targets == []
