import subprocess
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from maida_heal.models import FixesConfig
from maida_heal.prerequisites import (
    PrerequisiteError,
    check_fixer,
    check_gh_auth,
    materialize_config_repo,
)
from maida_heal.state import StateStore


def git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def repository(path: Path, slug: str = "maida-ai/example") -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Prerequisite Tests")
    git(path, "config", "user.email", "prerequisites@example.test")
    git(path, "remote", "add", "origin", f"git@github.com:{slug}.git")
    git(path, "commit", "--allow-empty", "-m", "initial")
    return path


def test_fixer_prerequisites_are_explicit_config_checks(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr("maida_heal.prerequisites.shutil.which", lambda _name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    check_fixer("command", ["fixture"])
    with pytest.raises(PrerequisiteError, match="ANTHROPIC_API_KEY"):
        check_fixer("api", None)
    with pytest.raises(PrerequisiteError, match=r"fixes\.command"):
        check_fixer("command", None)
    with pytest.raises(PrerequisiteError, match="claude-code, api, or command"):
        check_fixer("unknown", None)
    with pytest.raises(PrerequisiteError, match="Claude Code was not found"):
        check_fixer("claude-code", None)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    check_fixer("api", None)


def test_github_auth_failure_has_copyable_remediation(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "maida_heal.prerequisites.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "denied"),
    )

    with pytest.raises(PrerequisiteError, match="gh auth status"):
        check_gh_auth()


def test_config_repository_accepts_a_local_path_and_persists_identity(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "control")
    repo = repository(tmp_path / "agent-config")
    fixes = FixesConfig(repo=str(repo), fixer="command", command=["fixture-fixer"])

    resolved = materialize_config_repo(state, fixes)

    assert resolved == repo.resolve()
    assert fixes.repo == "maida-ai/example"
    assert fixes.repo_local_path == str(repo.resolve())


def test_config_repository_slug_clones_to_deterministic_local_state(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "control")
    fixes = FixesConfig(
        repo="maida-ai/example", fixer="command", command=["fixture-fixer"]
    )
    calls: list[tuple[str, Path]] = []

    def clone(slug: str, destination: Path) -> None:
        calls.append((slug, destination))
        repository(destination, slug)

    resolved = materialize_config_repo(state, fixes, clone=clone)

    expected = state.root / "repositories" / "maida-ai-example"
    assert calls == [("maida-ai/example", expected)]
    assert resolved == expected.resolve()
    assert fixes.repo_local_path == str(expected.resolve())
