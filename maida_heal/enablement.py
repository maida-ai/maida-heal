"""Progressive tier configuration and capability checks."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from maida_heal.gitops import GitError, git_root, github_slug, localize
from maida_heal.models import FixesConfig, HealConfig
from maida_heal.state import StateStore


class EnablementError(ValueError):
    """A tier prerequisite is unavailable or failed its copy-pasteable check."""


def check_gh_auth() -> None:
    completed = subprocess.run(
        ["gh", "auth", "status"], text=True, capture_output=True, check=False
    )
    if completed.returncode != 0:
        raise EnablementError(
            "GitHub CLI authentication is required for pull requests. Run "
            "`gh auth login`, then verify with `gh auth status`."
        )


def clone_repository(slug: str, destination: Path) -> Path:
    if destination.exists():
        return git_root(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["gh", "repo", "clone", slug, str(destination)],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise EnablementError(
            f"could not clone {slug}: {completed.stderr.strip() or 'no diagnostic'}"
        )
    return git_root(destination)


def resolve_config_repo(state: StateStore, value: str) -> tuple[Path, str]:
    requested = Path(value).expanduser()
    if requested.exists():
        try:
            root = git_root(requested)
            return root, github_slug(root)
        except GitError as error:
            raise EnablementError(str(error)) from error
    if value.count("/") == 1 and not value.startswith((".", "/")):
        slug = value.removesuffix(".git")
        destination = state.root / "repositories" / slug.replace("/", "-")
        return clone_repository(slug, destination), slug
    raise EnablementError(
        "config repo must be an existing local git path or an OWNER/REPO GitHub slug"
    )


def select_fixer(requested: str | None, command: list[str] | None) -> str:
    if requested is not None:
        if requested not in {"claude-code", "api", "command"}:
            raise EnablementError("fixer must be claude-code, api, or command")
        selected = requested
    elif shutil.which("claude"):
        selected = "claude-code"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        selected = "api"
    elif command:
        selected = "command"
    else:
        raise EnablementError(
            "No fix writer was detected. Install Claude Code and verify with "
            "`claude --version`, export ANTHROPIC_API_KEY, or pass "
            "`--fixer command --command '...'`."
        )
    if selected == "claude-code" and not shutil.which("claude"):
        raise EnablementError(
            "Claude Code was not found. Install it, then run `claude --version`."
        )
    if selected == "api" and not os.environ.get("ANTHROPIC_API_KEY"):
        raise EnablementError(
            "ANTHROPIC_API_KEY is not set. Export it and run "
            '`test -n "$ANTHROPIC_API_KEY"`.'
        )
    if selected == "command" and not command:
        raise EnablementError("the command fixer requires --command")
    return selected


def enable_fixes(
    state: StateStore,
    config: HealConfig,
    *,
    repo_value: str,
    fixer_kind: str | None,
    command: list[str] | None,
    auth_check: Callable[[], None] = check_gh_auth,
) -> HealConfig:
    if config.tier < 1:
        raise EnablementError("Run `maida-heal up` before enabling fixes")
    if config.fixes is not None:
        raise EnablementError("fixes are already enabled")
    repo, slug = resolve_config_repo(state, repo_value)
    auth_check()
    selected = select_fixer(fixer_kind, command)
    config.fixes = FixesConfig(
        repo=slug,
        repo_local_path=str(repo),
        fixer=selected,
        command=command if selected == "command" else None,
    )
    for finding in state.list_findings(config):
        if finding.localization:
            continue
        finding.localization = localize(
            repo,
            onset=finding.onset_at or finding.detected_at,
            terms=[
                finding.stream,
                *finding.metric_names,
                *(
                    term
                    for failure in finding.metric_failures
                    for term in failure.structural_terms
                ),
            ],
        )
        state.save_finding(finding, config)
    state.save_config(config)
    return config
