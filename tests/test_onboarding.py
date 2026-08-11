import stat
from datetime import datetime, timezone
from pathlib import Path

from maida_heal.core import MaidaCLI
from maida_heal.onboarding import (
    apply_stream_edits,
    attach,
    fixture_attachment_client,
    purge_imported_data,
    watch_once,
)
from maida_heal.state import StateStore

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def test_fixture_up_is_complete_shadow_profile_and_first_report_is_immediate(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path)
    batch, client = fixture_attachment_client()
    progress: list[str] = []

    result = attach(
        state,
        MaidaCLI(state),
        client,
        host="fixture://langfuse",
        credential_source="fixture",
        now=NOW,
        metadata_keys=["agent_id"],
        configure=lambda candidates: apply_stream_edits(candidates, select_all=True),
        progress=progress.append,
        fixture_batch=batch,
    )

    assert result.traces == 19
    assert len(result.streams) == 1
    assert result.detections[0].verdict == "fail"
    assert client.validation_calls == 1
    assert client.discovery_windows == [(result.window_start, result.window_end)]
    assert any(line.startswith("Connect") for line in progress)
    assert any(line.startswith("Report") for line in progress)
    assert all(
        finding.onset_at == batch[14].started_at for finding in state.list_findings()
    )
    config_text = state.config_path.read_text(encoding="utf-8")
    assert "secret" not in config_text.lower()
    assert "public_key" not in config_text.lower()
    assert stat.S_IMODE(state.root.stat().st_mode) == 0o700


def test_watch_is_idempotent_and_deduplicates_existing_findings(tmp_path: Path) -> None:
    state = StateStore(tmp_path)
    batch, client = fixture_attachment_client()
    attach(
        state,
        MaidaCLI(state),
        client,
        host="fixture://langfuse",
        credential_source="fixture",
        now=NOW,
        metadata_keys=["agent_id"],
        configure=lambda candidates: apply_stream_edits(candidates, select_all=True),
        progress=lambda _line: None,
        fixture_batch=batch,
    )
    before = {item.id for item in state.list_findings()}
    _, second_client = fixture_attachment_client()

    result = watch_once(
        state,
        MaidaCLI(state),
        second_client,
        now=NOW,
        progress=lambda _line: None,
        fixture_batch=batch,
    )

    assert result.traces == 19
    assert {item.id for item in state.list_findings()} == before


def test_purge_removes_imported_data_but_preserves_findings(tmp_path: Path) -> None:
    state = StateStore(tmp_path)
    batch, client = fixture_attachment_client()
    attach(
        state,
        MaidaCLI(state),
        client,
        host="fixture://langfuse",
        credential_source="fixture",
        now=NOW,
        metadata_keys=["agent_id"],
        configure=lambda candidates: apply_stream_edits(candidates, select_all=True),
        progress=lambda _line: None,
        fixture_batch=batch,
    )
    findings = state.list_findings()

    removed = purge_imported_data(state)

    assert removed > 0
    assert not state.data_dir.exists()
    assert state.list_findings() == findings
