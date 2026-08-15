"""Noninteractive checks for config-declared external prerequisites."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from maida.heal.gitops import GitError, git_root, github_slug
from maida.heal.models import FixesConfig
from maida.heal.state import StateStore


class PrerequisiteError(ValueError):
    """A declared external prerequisite is unavailable or invalid."""


Clone = Callable[[str, Path], None]


def _clone_repository(slug: str, destination: Path) -> None:
    completed = subprocess.run(
        ["gh", "repo", "clone", slug, str(destination)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PrerequisiteError(
            f"could not clone {slug}: {completed.stderr.strip() or 'no diagnostic'}"
        )


def materialize_config_repo(
    state: StateStore,
    fixes: FixesConfig,
    *,
    clone: Clone | None = None,
) -> Path:
    """Resolve a local path or clone a configured GitHub slug without prompting."""
    state.initialize()
    explicit = fixes.repo_local_path
    requested: Path | None = None
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        requested = (
            candidate if candidate.is_absolute() else state.project_root / candidate
        )
        if not requested.exists():
            raise PrerequisiteError(
                f"fixes.repo_local_path does not exist: {requested.resolve()}"
            )
    else:
        candidate = Path(fixes.repo).expanduser()
        local_candidate = (
            candidate if candidate.is_absolute() else state.project_root / candidate
        )
        if local_candidate.exists():
            requested = local_candidate

    if requested is None:
        slug = fixes.repo.removesuffix(".git").strip("/")
        if slug.count("/") != 1 or fixes.repo.startswith((".", "/")):
            raise PrerequisiteError(
                "fixes.repo must be an existing local git path or OWNER/REPO slug"
            )
        destination = state.root / "repositories" / slug.replace("/", "-")
        if not destination.exists():
            (clone or _clone_repository)(slug, destination)
        requested = destination

    try:
        root = git_root(requested)
        discovered_slug = github_slug(root)
    except GitError as error:
        raise PrerequisiteError(str(error)) from error
    configured_slug = fixes.repo.removesuffix(".git").strip("/")
    if (
        configured_slug.count("/") == 1
        and not fixes.repo.startswith((".", "/"))
        and configured_slug != discovered_slug
    ):
        raise PrerequisiteError(
            f"fixes.repo is {configured_slug}, but the local origin is "
            f"{discovered_slug}"
        )
    fixes.repo = discovered_slug
    fixes.repo_local_path = str(root)
    return root


def check_gh_auth() -> None:
    completed = subprocess.run(
        ["gh", "auth", "status"], text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise PrerequisiteError(
            "GitHub CLI authentication is required for pull requests. Run "
            "`gh auth login`, then verify with `gh auth status`."
        )


def check_fixer(kind: str, command: list[str] | None) -> None:
    if kind not in {"claude-code", "api", "command"}:
        raise PrerequisiteError("fixes.fixer must be claude-code, api, or command")
    if kind == "claude-code" and not shutil.which("claude"):
        raise PrerequisiteError(
            "Claude Code was not found. Install it, then run `claude --version`."
        )
    if kind == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise PrerequisiteError(
            "ANTHROPIC_API_KEY is not set. Export it and run "
            '`test -n "$ANTHROPIC_API_KEY"`.'
        )
    if kind == "command" and not command:
        raise PrerequisiteError("the command fixer requires fixes.command")
