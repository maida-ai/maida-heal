import subprocess
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maida_heal.killswitch import KillSwitchSyncError, sync_ci_kill_switch
from maida_heal.models import FixesConfig, GateConfig, HealConfig, LangfuseConfig
from maida_heal.state import (
    StateStore,
    clear_kill_switch,
    write_kill_switch,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def config(repo: Path, *, gate: bool = True) -> HealConfig:
    return HealConfig(
        langfuse=LangfuseConfig(
            host="https://example.test", credential_source="environment"
        ),
        fixes=FixesConfig(
            repo="maida-ai/example",
            repo_local_path=str(repo),
            fixer="command",
            command=["fixture"],
        ),
        gate=(
            GateConfig(
                command=["gate", "{report}"],
                holdout_command=["holdout", "{report}"],
            )
            if gate
            else None
        ),
    )


def test_local_lock_is_mirrored_for_gate_and_cleared_together(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = StateStore(tmp_path / "control")
    selected = config(repo)
    payload = {
        "schema_version": "1.0.0",
        "paused_at": NOW.isoformat(),
        "actor": "human",
    }

    write_kill_switch(state, selected, payload)

    mirror = repo / ".maida" / "heal.lock"
    assert state.lock_path.is_file()
    assert mirror.is_file()
    clear_kill_switch(state, selected)
    assert not state.lock_path.exists()
    assert not mirror.exists()


def test_ci_variable_sync_is_tier_gated_and_uses_explicit_repo(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        typed = list(command)
        calls.append(typed)
        return subprocess.CompletedProcess(typed, 0, "", "")

    assert sync_ci_kill_switch(config(tmp_path, gate=False), paused=True) is False
    assert sync_ci_kill_switch(config(tmp_path), paused=True, runner=runner) is True
    assert calls[0] == [
        "gh",
        "variable",
        "set",
        "MAIDA_HEAL_PAUSED",
        "--body",
        "true",
        "--repo",
        "maida-ai/example",
    ]


def test_ci_variable_failure_keeps_a_clear_manual_diagnostic(tmp_path: Path) -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 1, "", "denied")

    with pytest.raises(KillSwitchSyncError, match="denied"):
        sync_ci_kill_switch(config(tmp_path), paused=False, runner=runner)
