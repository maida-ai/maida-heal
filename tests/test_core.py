import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maida_heal.core import (
    CommandResult,
    MaidaCLI,
    ReportCompatibilityError,
    validate_report,
)
from maida_heal.state import StateStore


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        del cwd
        self.calls.append((tuple(args), dict(env)))
        return CommandResult(
            tuple(args),
            0,
            json.dumps({"imported": [], "skipped": []}),
            "",
        )


def test_langfuse_import_always_passes_absolute_bounds_and_redacts_payloads(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    cli = MaidaCLI(StateStore(tmp_path), runner=runner, executable="maida")
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 15, tzinfo=timezone.utc)

    cli.import_langfuse(from_time=start, to_time=end, host="https://example.test")

    args, env = runner.calls[0]
    assert args[1:3] == ("import", "langfuse")
    assert args[args.index("--from") + 1] == "2026-08-01T00:00:00+00:00"
    assert args[args.index("--to") + 1] == "2026-08-15T00:00:00+00:00"
    assert "--since" not in args
    assert {"input", "output", "prompt", "result"} <= set(
        env["MAIDA_REDACT_KEYS"].split(",")
    )
    assert env["MAIDA_DATA_DIR"].startswith(str(tmp_path))


def test_report_major_mismatch_refuses_cleanly() -> None:
    with pytest.raises(
        ReportCompatibilityError, match="unsupported Maida report major"
    ):
        validate_report(
            {
                "report_version": "3.0.0",
                "verdict": "pass",
                "aggregate_results": [],
            }
        )
