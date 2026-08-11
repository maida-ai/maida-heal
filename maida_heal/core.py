"""Public CLI boundary to the pinned Maida verifier.

No verifier code is imported. Every verdict is produced by `maida` 0.5.0 and
consumed through its documented exit codes and report schema 2.x.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from maida_heal.constants import (
    EXIT_GATE_FAILED,
    EXIT_NOT_FOUND,
    PAYLOAD_REDACTION_KEYS,
    SUPPORTED_MAIDA_REPORT_MAJOR,
)
from maida_heal.state import StateStore, read_json


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult: ...


class SubprocessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        completed = subprocess.run(
            list(args),
            cwd=cwd,
            env=dict(env),
            text=True,
            capture_output=True,
            check=False,
        )
        return CommandResult(
            args=tuple(args),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class CoreCommandError(RuntimeError):
    def __init__(self, message: str, result: CommandResult) -> None:
        super().__init__(message)
        self.result = result


class ReportCompatibilityError(ValueError):
    """A Maida report cannot be consumed without risking semantic drift."""


def validate_report(payload: dict[str, object]) -> dict[str, object]:
    version = payload.get("report_version")
    if not isinstance(version, str):
        raise ReportCompatibilityError("Maida report has no semantic report_version")
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ReportCompatibilityError(
            f"Maida report_version must use major.minor.patch form: {version!r}"
        )
    if int(parts[0]) != SUPPORTED_MAIDA_REPORT_MAJOR:
        raise ReportCompatibilityError(
            f"unsupported Maida report major {parts[0]}; maida-heal supports "
            f"report major {SUPPORTED_MAIDA_REPORT_MAJOR}"
        )
    if payload.get("verdict") not in {"pass", "fail", "inconclusive"}:
        raise ReportCompatibilityError("Maida report has an invalid verdict")
    if not isinstance(payload.get("aggregate_results"), list):
        raise ReportCompatibilityError("Maida report has no aggregate_results array")
    return payload


class MaidaCLI:
    """Drive only documented Maida commands with isolated, redacted storage."""

    def __init__(
        self,
        state: StateStore,
        *,
        runner: CommandRunner | None = None,
        executable: str | None = None,
    ) -> None:
        self.state = state
        self.runner = runner or SubprocessRunner()
        selected = executable or os.environ.get("MAIDA_HEAL_MAIDA", "").strip()
        self.executable = selected or shutil.which("maida") or "maida"

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "MAIDA_DATA_DIR": str(self.state.data_dir),
                "MAIDA_REDACT": "true",
                "MAIDA_REDACT_KEYS": ",".join(PAYLOAD_REDACTION_KEYS),
                "MAIDA_MAX_FIELD_BYTES": "100",
            }
        )
        if extra:
            env.update(extra)
        return env

    def _run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        extra_env: Mapping[str, str] | None = None,
        accepted: frozenset[int] = frozenset({0}),
    ) -> CommandResult:
        result = self.runner.run(
            [self.executable, *arguments],
            cwd=cwd or self.state.project_root,
            env=self.environment(extra_env),
        )
        if result.returncode not in accepted:
            message = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise CoreCommandError(
                f"Maida command failed with exit {result.returncode}: {message}", result
            )
        return result

    @staticmethod
    def _absolute_time(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Langfuse import bounds must be timezone-aware")
        return value.isoformat()

    def import_langfuse(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        host: str,
    ) -> dict[str, object]:
        if to_time <= from_time:
            raise ValueError("Langfuse import end must be after its start")
        result = self._run(
            [
                "import",
                "langfuse",
                "--from",
                self._absolute_time(from_time),
                "--to",
                self._absolute_time(to_time),
                "--base-url",
                host,
                "--json",
            ],
            accepted=frozenset({0, EXIT_NOT_FOUND}),
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CoreCommandError(
                "Maida import returned invalid JSON", result
            ) from error
        if not isinstance(payload, dict):
            raise CoreCommandError("Maida import returned a non-object summary", result)
        if result.returncode == EXIT_NOT_FOUND and not payload.get("skipped"):
            raise CoreCommandError(
                "No complete Langfuse traces matched the window", result
            )
        return payload

    def list_runs(self, *, limit: int = 10000) -> list[dict[str, object]]:
        result = self._run(["list", "--limit", str(limit), "--json"])
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CoreCommandError(
                "Maida list returned invalid JSON", result
            ) from error
        runs = payload.get("runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list) or not all(
            isinstance(item, dict) for item in runs
        ):
            raise CoreCommandError("Maida list returned an invalid runs array", result)
        return runs

    def extract(
        self,
        *,
        window: Path,
        out_dir: Path,
        workflow: str,
    ) -> dict[str, object]:
        result = self._run(
            [
                "extract",
                "--window",
                str(window),
                "--out",
                str(out_dir),
                "--workflow",
                workflow,
                "--json",
            ]
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise CoreCommandError(
                "Maida extract returned invalid JSON", result
            ) from error
        if not isinstance(payload, dict):
            raise CoreCommandError(
                "Maida extract returned a non-object manifest", result
            )
        return payload

    def drift(
        self,
        *,
        window: Path,
        baseline: Path,
        policy: Path,
        agent: str,
        report_path: Path,
    ) -> dict[str, object]:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            [
                "drift",
                "--window",
                str(window),
                "--baseline",
                str(baseline),
                "--policy",
                str(policy),
                "--agent",
                agent,
                "--format",
                "json",
                "--json-out",
                str(report_path),
            ],
            accepted=frozenset({0, EXIT_GATE_FAILED}),
        )
        try:
            payload = read_json(report_path)
        except ValueError as error:
            raise CoreCommandError(
                "Maida drift did not write a valid report", result
            ) from error
        return validate_report(payload)
