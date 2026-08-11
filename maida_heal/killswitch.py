"""Synchronize the local kill switch with the generated GitHub workflow."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from typing import Protocol

from maida_heal.models import HealConfig


class KillSwitchSyncError(RuntimeError):
    """The local lock is safe, but the remote CI variable could not be updated."""


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
    """Set the Actions variable checked by the tier-3 workflow, when present."""
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
            "true" if paused else "false",
            "--repo",
            config.fixes.repo,
        ]
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "no diagnostic"
        raise KillSwitchSyncError(
            "local kill switch changed, but the GitHub Actions variable did not: "
            f"{detail}"
        )
    return True
