import subprocess
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from maida_heal.enablement import (
    EnablementError,
    enable_fixes,
    resolve_config_repo,
    select_fixer,
)
from maida_heal.models import HealConfig, LangfuseConfig
from maida_heal.state import StateStore


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Enable Tests")
    git(path, "config", "user.email", "enable@example.test")
    git(path, "remote", "add", "origin", "git@github.com:maida-ai/example.git")
    (path / "AGENTS.md").write_text("fixture\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "--allow-empty", "-m", "initial")
    return path


def tier_one() -> HealConfig:
    return HealConfig(
        langfuse=LangfuseConfig(
            host="https://example.test", credential_source="environment"
        )
    )


def test_enable_fixes_resolves_repo_then_checks_auth_and_persists_one_section(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "control")
    config = tier_one()
    state.save_config(config)
    repo = repository(tmp_path / "repo")
    checked: list[bool] = []

    enabled = enable_fixes(
        state,
        config,
        repo_value=str(repo),
        fixer_kind="command",
        command=["./fixture-fixer"],
        auth_check=lambda: checked.append(True),
    )

    assert checked == [True]
    assert enabled.tier == 2
    assert enabled.fixes and enabled.fixes.repo == "maida-ai/example"
    text = state.config_path.read_text(encoding="utf-8")
    assert "fixes:" in text
    assert "gate:" not in text
    assert "auto_merge:" not in text


def test_tier_and_repo_validation_fail_before_any_external_action(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "control")
    calls: list[bool] = []
    with pytest.raises(EnablementError, match="maida-heal up"):
        enable_fixes(
            state,
            HealConfig(),
            repo_value=str(tmp_path),
            fixer_kind="command",
            command=["fixture"],
            auth_check=lambda: calls.append(True),
        )
    assert calls == []

    with pytest.raises(EnablementError, match="existing local git path"):
        resolve_config_repo(state, str(tmp_path / "missing"))


def test_fixer_detection_and_credentials_are_explicit(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("maida_heal.enablement.shutil.which", lambda _name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert select_fixer("command", ["fixture"]) == "command"
    with pytest.raises(EnablementError, match="ANTHROPIC_API_KEY"):
        select_fixer("api", None)
    with pytest.raises(EnablementError, match="requires --command"):
        select_fixer("command", None)
    with pytest.raises(EnablementError, match="claude-code, api, or command"):
        select_fixer("unknown", None)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    assert select_fixer(None, None) == "api"
