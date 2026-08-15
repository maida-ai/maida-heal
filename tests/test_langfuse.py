import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest
from pytest import MonkeyPatch

from maida.heal.cli import _resolve_and_bridge_credentials
from maida.heal.discovery import discover_streams
from maida.heal.langfuse import (
    HTTPClient,
    LangfuseCredentials,
    LangfuseError,
    resolve_credentials,
)

FIXTURE = Path(__file__).parent / "fixtures" / "langfuse-observations-v2.json"
START = datetime(2026, 8, 1, tzinfo=timezone.utc)
END = datetime(2026, 8, 15, tzinfo=timezone.utc)


class RecordedClient(HTTPClient):
    def __init__(self) -> None:
        super().__init__(
            LangfuseCredentials("public", "secret", "https://example.test", "test")
        )
        self.requests: list[dict[str, object]] = []

    def _get(self, params: Mapping[str, object]) -> dict[str, object]:
        self.requests.append(dict(params))
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        return cast(dict[str, object], payload)


def test_recorded_v2_shape_uses_absolute_window_and_groups_mixed_streams() -> None:
    client = RecordedClient()
    observations = client.discover(
        from_time=START,
        to_time=END,
        metadata_keys=["agent_id"],
    )
    streams = discover_streams(observations, metadata_keys=["agent_id"])

    assert len(observations) == 6
    assert observations[0].start_time == START
    assert observations[-1].start_time == datetime(
        2026, 8, 14, 23, 59, 59, tzinfo=timezone.utc
    )
    request = client.requests[0]
    assert request["fromStartTime"] == START.isoformat()
    assert request["toStartTime"] == END.isoformat()
    assert [(item.grouping, item.trace_count) for item in streams] == [
        ("metadata", 2),
        ("session_pattern", 2),
        ("trace_name", 2),
    ]
    assert "PII-recorded-shape-sentinel" not in repr(observations)
    assert all("billing-private-value" not in item.name for item in streams)


def test_credentials_prefer_environment_then_safe_local_config(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr("maida.heal.langfuse.Path.home", lambda: tmp_path / "home")
    environment = {
        "LANGFUSE_PUBLIC_KEY": "pk-env",
        "LANGFUSE_SECRET_KEY": "sk-env",
        "LANGFUSE_HOST": "https://env.example.test/",
    }
    selected = resolve_credentials(tmp_path, environ=environment)
    assert selected.host == "https://env.example.test"
    assert selected.source == "environment"

    (tmp_path / ".env").write_text(
        "export LANGFUSE_PUBLIC_KEY='pk-file'\n"
        'LANGFUSE_SECRET_KEY="sk-file"\n'
        "LANGFUSE_BASE_URL=https://file.example.test/\n"
        "NOT VALID=$(do-not-evaluate)\n",
        encoding="utf-8",
    )
    selected = resolve_credentials(tmp_path, environ={})
    assert selected.public_key == "pk-file"
    assert selected.secret_key == "sk-file"
    assert selected.host == "https://file.example.test"
    assert selected.source == "config"


def test_missing_credentials_and_invalid_windows_fail_cleanly(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr("maida.heal.langfuse.Path.home", lambda: tmp_path / "home")
    with pytest.raises(LangfuseError, match="Project settings"):
        resolve_credentials(tmp_path, environ={})
    client = RecordedClient()
    with pytest.raises(ValueError, match="timezone-aware"):
        client.discover(
            from_time=datetime(2026, 8, 1),
            to_time=END,
            metadata_keys=[],
        )
    with pytest.raises(ValueError, match="after its start"):
        client.discover(from_time=END, to_time=START, metadata_keys=[])
    with pytest.raises(LangfuseError, match=r"http\(s\) origin"):
        HTTPClient(LangfuseCredentials("pk", "sk", "file:///tmp", "test"))


def test_file_credentials_are_bridged_only_to_the_import_child_environment(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr("maida.heal.langfuse.Path.home", lambda: tmp_path / "home")
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "LANGFUSE_PUBLIC_KEY=pk-file\n"
        "LANGFUSE_SECRET_KEY=sk-file\n"
        "LANGFUSE_HOST=https://file.example.test\n",
        encoding="utf-8",
    )

    selected = _resolve_and_bridge_credentials(tmp_path)

    assert selected.source == "config"
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-file"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-file"
