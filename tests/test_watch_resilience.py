from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pytest import MonkeyPatch

from maida.heal.core import MaidaCLI
from maida.heal.events import EventJournal
from maida.heal.fixtures import FixtureRun
from maida.heal.models import StreamConfig
from maida.heal.onboarding import (
    apply_stream_edits,
    attach,
    fixture_attachment_client,
    watch_once,
)
from maida.heal.state import StateStore

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def attached_state(root: Path) -> tuple[StateStore, list[FixtureRun]]:
    state = StateStore(root)
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
    return state, batch


def test_stream_comparison_failure_is_isolated_and_persisted(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, batch = attached_state(tmp_path)
    config = state.load_config()
    original = config.streams[0]
    second = StreamConfig.model_validate(
        {
            **original.model_dump(mode="json"),
            "id": "support-agent-secondary",
            "name": "Support agent secondary",
        }
    )
    config.streams.append(second)
    state.save_config(config)
    index = state.load_imports()
    index.stream_cursors[second.id] = NOW - timedelta(days=14)
    state.save_imports(index)

    from maida.heal import onboarding

    real_evaluate = onboarding.evaluate_stream

    def isolated_failure(*args: object, **kwargs: object) -> object:
        stream = args[3]
        assert isinstance(stream, StreamConfig)
        if stream.id == original.id:
            raise RuntimeError("SENTINEL_CUSTOMER_PII")
        return real_evaluate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(onboarding, "evaluate_stream", isolated_failure)
    _, client = fixture_attachment_client()

    result = watch_once(
        state,
        MaidaCLI(state),
        client,
        now=NOW + timedelta(minutes=1),
        progress=lambda _line: None,
        fixture_batch=batch,
    )

    assert [item.stream_id for item in result.errors] == [original.id]
    assert [item.stream_id for item in result.detections] == [second.id]
    health = state.load_health()
    assert health.streams[original.id].status == "degraded"
    assert health.streams[second.id].status == "healthy"
    persisted = state.health_path.read_text()
    assert "SENTINEL_CUSTOMER_PII" not in persisted


def test_stream_import_failure_does_not_stop_other_streams(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, batch = attached_state(tmp_path)
    config = state.load_config()
    original = config.streams[0]
    second = StreamConfig.model_validate(
        {
            **original.model_dump(mode="json"),
            "id": "support-agent-secondary",
            "name": "Support agent secondary",
        }
    )
    config.streams.append(second)
    state.save_config(config)

    from maida.heal import onboarding

    real_builder = onboarding.build_import_records

    def isolated_failure(*args: object, **kwargs: object) -> object:
        streams = args[3]
        assert isinstance(streams, list)
        stream = streams[0]
        assert isinstance(stream, StreamConfig)
        if stream.id == original.id:
            raise RuntimeError("SENTINEL_CUSTOMER_PII")
        return real_builder(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(onboarding, "build_import_records", isolated_failure)
    _, client = fixture_attachment_client()
    next_cycle = NOW + timedelta(minutes=1)

    result = watch_once(
        state,
        MaidaCLI(state),
        client,
        now=next_cycle,
        progress=lambda _line: None,
        fixture_batch=batch,
    )

    assert [(item.stream_id, item.phase) for item in result.errors] == [
        (original.id, "import")
    ]
    index = state.load_imports()
    assert original.id not in index.stream_cursors
    assert index.stream_cursors[second.id] == next_cycle
    assert state.load_health().streams[second.id].status == "healthy"
    assert "SENTINEL_CUSTOMER_PII" not in state.health_path.read_text()


def test_forced_process_kill_after_cursor_checkpoint_restarts_without_duplicates(
    tmp_path: Path,
) -> None:
    state, _batch = attached_state(tmp_path)
    config = state.load_config()
    journal = EventJournal(tmp_path, config.events)
    journal.flush()
    before_ids = {
        json.loads(line)["event_id"]
        for line in (tmp_path / ".maida-heal" / "events.jsonl").read_text().splitlines()
    }
    script = """
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from maida.heal.core import MaidaCLI
from maida.heal.onboarding import fixture_attachment_client, watch_once
from maida.heal.state import StateStore
root = Path(sys.argv[1])
state = StateStore(root)
batch, client = fixture_attachment_client()
def checkpoint(name):
    if name == 'imports_persisted':
        os.kill(os.getpid(), signal.SIGKILL)
watch_once(
    state, MaidaCLI(state), client,
    now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
    progress=lambda _line: None,
    fixture_batch=batch,
    checkpoint=checkpoint,
)
"""

    killed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert killed.returncode == -9
    batch, client = fixture_attachment_client()
    watch_once(
        state,
        MaidaCLI(state),
        client,
        now=NOW,
        progress=lambda _line: None,
        fixture_batch=batch,
    )
    state.reconcile_finding_events(config)
    journal.flush()
    index = state.load_imports()
    assert len(index.records) == 19
    after = [
        json.loads(line)["event_id"]
        for line in (tmp_path / ".maida-heal" / "events.jsonl").read_text().splitlines()
    ]
    assert len(after) == len(set(after))
    assert set(after) == before_ids


def test_import_cursor_uses_absolute_overlap_window(tmp_path: Path) -> None:
    state, batch = attached_state(tmp_path)
    _, client = fixture_attachment_client()
    later = NOW + timedelta(minutes=10)

    watch_once(
        state,
        MaidaCLI(state),
        client,
        now=later,
        progress=lambda _line: None,
        fixture_batch=batch,
    )

    assert client.discovery_windows[-1] == (
        NOW - timedelta(minutes=5),
        later,
    )
