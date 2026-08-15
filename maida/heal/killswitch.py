"""Synchronize the local kill switch with the generated GitHub workflow."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, Protocol

from maida.heal.models import HealConfig


class KillSwitchSyncError(RuntimeError):
    """The local lock is safe, but the remote CI variable could not be updated."""


def lock_scope(path: Path) -> Literal["all", "fix_dispatch"] | None:
    """Read a scoped lock; malformed or legacy locks fail closed."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "all"
    if isinstance(payload, dict) and payload.get("scope") == "fix_dispatch":
        return "fix_dispatch"
    return "all"


def lock_blocks(path: Path, *, operation: str) -> bool:
    scope = lock_scope(path)
    return scope == "all" or (
        scope == "fix_dispatch"
        and operation
        in {
            "fix_dispatch",
            "auto_merge",
        }
    )


class VariableRunner(Protocol):
    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]: ...


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), text=True, capture_output=True, check=False)


def sync_ci_kill_switch(
    config: HealConfig,
    *,
    paused: bool,
    runner: VariableRunner | None = None,
) -> bool:
    """Synchronize a manual all-scope pause or resume."""
    return sync_ci_pause_scope(
        config,
        scope="all" if paused else "none",
        runner=runner,
    )


def sync_ci_pause_scope(
    config: HealConfig,
    *,
    scope: Literal["none", "all", "fix_dispatch"],
    runner: VariableRunner | None = None,
) -> bool:
    """Set the Actions pause scope without storing credentials or remote state."""
    if config.gate is None or config.fixes is None:
        return False
    execute: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = (
        runner or _run
    )
    completed = execute(
        [
            "gh",
            "variable",
            "set",
            "MAIDA_HEAL_PAUSED",
            "--body",
            {"none": "false", "all": "true", "fix_dispatch": "fix_dispatch"}[scope],
            "--repo",
            config.fixes.repo,
        ]
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "no diagnostic"
        raise KillSwitchSyncError(
            "local pause scope changed, but the GitHub Actions variable did not: "
            f"{detail}"
        )
    return True
