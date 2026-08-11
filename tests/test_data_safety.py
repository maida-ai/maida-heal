from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from maida_heal.core import MaidaCLI
from maida_heal.discovery import discover_streams
from maida_heal.fixtures import fixture_observations, fixture_runs
from maida_heal.healing import _pr_body, fixer_prompt
from maida_heal.langfuse import HTTPClient, LangfuseCredentials, Observation
from maida_heal.models import FixesConfig
from maida_heal.onboarding import apply_stream_edits, attach
from maida_heal.state import StateStore

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
SENTINEL = "PII-customer@example.test-card-4111111111111111"


class PayloadResponseClient(HTTPClient):
    def __init__(self) -> None:
        super().__init__(
            LangfuseCredentials("public", "secret", "https://example.test", "test")
        )
        self.parameters: list[dict[str, object]] = []

    def _get(self, params: Mapping[str, object]) -> dict[str, object]:
        self.parameters.append(dict(params))
        return {
            "data": [
                {
                    "id": "observation-1",
                    "traceId": "trace-1",
                    "traceName": "support-agent",
                    "startTime": "2026-08-10T12:00:00Z",
                    "endTime": "2026-08-10T12:00:01Z",
                    "type": "GENERATION",
                    "name": "respond",
                    "metadata": {"agent_id": "support"},
                    "input": SENTINEL,
                    "output": SENTINEL,
                }
            ],
            "meta": {},
        }


def test_discovery_requests_no_io_fields_and_drops_payload_content() -> None:
    client = PayloadResponseClient()
    observations = client.discover(
        from_time=datetime(2026, 8, 1, tzinfo=timezone.utc),
        to_time=NOW,
        metadata_keys=["agent_id"],
    )

    assert len(observations) == 1
    assert SENTINEL not in repr(observations)
    fields = str(client.parameters[0]["fields"])
    assert "io" not in fields
    assert "input" not in fields
    assert "output" not in fields


def test_trace_metadata_value_never_reaches_persisted_or_outbound_surfaces(
    tmp_path: Path,
) -> None:
    batch = fixture_runs(regression=True)
    observations = [
        replace(item, session_id=None, metadata={"agent_id": SENTINEL})
        for item in fixture_observations(batch)
    ]

    class Client:
        def validate(self) -> None:
            return None

        def discover(
            self,
            *,
            from_time: datetime,
            to_time: datetime,
            metadata_keys: Iterable[str],
        ) -> list[Observation]:
            del from_time, to_time, metadata_keys
            return observations

    state = StateStore(tmp_path)
    progress: list[str] = []
    attach(
        state,
        MaidaCLI(state),
        Client(),
        host="fixture://langfuse",
        credential_source="fixture",
        now=NOW,
        metadata_keys=["agent_id"],
        configure=lambda candidates: apply_stream_edits(candidates, select_all=True),
        progress=progress.append,
        fixture_batch=batch,
    )
    config = state.load_config()
    config.fixes = FixesConfig(
        repo="maida-ai/example",
        repo_local_path=str(tmp_path / "repo"),
        fixer="command",
        command=["fixture-fixer"],
    )
    state.save_config(config)
    item = state.list_findings(config)[0]
    candidates = discover_streams(observations, metadata_keys=["agent_id"])

    serialized_findings = json.dumps(
        [finding.model_dump(mode="json") for finding in state.list_findings(config)]
    )
    assert SENTINEL not in state.config_path.read_text(encoding="utf-8")
    assert SENTINEL not in serialized_findings
    assert SENTINEL not in "\n".join(progress)
    assert SENTINEL not in fixer_prompt(state, config, item)
    assert SENTINEL not in _pr_body(item, gate_enabled=False)
    assert SENTINEL not in candidates[0].name
