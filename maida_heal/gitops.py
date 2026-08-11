"""Exact-target git operations and the mandatory post-hoc diff boundary."""

from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

from maida_heal.constants import PROTECTED_PATH_PATTERNS
from maida_heal.models import LocalizationCandidate


class GitError(RuntimeError):
    """A bounded local git operation failed."""


def _run_git(
    repo: Path,
    arguments: list[str],
    *,
    accepted: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode not in accepted:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise GitError(f"git {' '.join(arguments[:2])} failed: {detail}")
    return completed


def git_root(path: Path) -> Path:
    completed = _run_git(path, ["rev-parse", "--show-toplevel"])
    root = Path(completed.stdout.strip()).resolve()
    if not root.is_dir():
        raise GitError(f"git returned an invalid repository root: {root}")
    return root


def github_slug(repo: Path) -> str:
    completed = _run_git(repo, ["remote", "get-url", "origin"])
    remote = completed.stdout.strip()
    patterns = (
        "git@github.com:",
        "ssh://git@github.com/",
        "https://github.com/",
        "http://github.com/",
    )
    for prefix in patterns:
        if remote.startswith(prefix):
            slug = remote[len(prefix) :].removesuffix(".git").strip("/")
            if slug.count("/") == 1:
                return slug
    raise GitError("the config repository origin is not a GitHub repository")


def _normal_path(value: str) -> str:
    normalized = value.replace(os.sep, "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"unsafe changed path: {value}")
    return path.as_posix()


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3].rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    return fnmatch.fnmatchcase(path, pattern)


@dataclass(frozen=True)
class DiffInspection:
    changed_paths: tuple[str, ...]
    diff_lines: int
    diff: str


@dataclass(frozen=True)
class DiffViolation:
    offending_paths: tuple[str, ...]
    reason: str


def validate_changed_paths(
    changed_paths: tuple[str, ...], allowed_patterns: list[str]
) -> DiffViolation | None:
    offending: list[str] = []
    protected: list[str] = []
    for raw in changed_paths:
        path = _normal_path(raw)
        if any(_matches(path, pattern) for pattern in PROTECTED_PATH_PATTERNS):
            protected.append(path)
            continue
        if not any(_matches(path, pattern) for pattern in allowed_patterns):
            offending.append(path)
    if protected:
        return DiffViolation(
            offending_paths=tuple(sorted(protected)),
            reason="protected paths are immutable to every fixer",
        )
    if offending:
        return DiffViolation(
            offending_paths=tuple(sorted(offending)),
            reason="paths are outside the configured fixer allowlist",
        )
    return None


def writable_symlinks(worktree: Path, allowed_patterns: list[str]) -> tuple[str, ...]:
    """List pre-existing symlinks a writer could use to escape writable paths."""
    hazards: list[str] = []
    for current, directories, files in os.walk(worktree, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            path = current_path / name
            if not path.is_symlink():
                continue
            relative = _normal_path(path.relative_to(worktree).as_posix())
            if any(_matches(relative, pattern) for pattern in allowed_patterns):
                hazards.append(relative)
    return tuple(sorted(hazards))


def changed_symlink_violation(
    worktree: Path, changed_paths: tuple[str, ...]
) -> DiffViolation | None:
    symlinks = tuple(path for path in changed_paths if (worktree / path).is_symlink())
    if not symlinks:
        return None
    return DiffViolation(
        offending_paths=symlinks,
        reason="fixers may not add or modify symbolic links",
    )


def inspect_diff(worktree: Path) -> DiffInspection:
    status = _run_git(
        worktree,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    ).stdout
    entries = [entry for entry in status.split("\0") if entry]
    paths: list[str] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if len(entry) < 4:
            index += 1
            continue
        status_code = entry[:2]
        value = entry[3:]
        paths.append(value)
        if ("R" in status_code or "C" in status_code) and index + 1 < len(entries):
            index += 1
            paths.append(entries[index])
        index += 1
    normalized = tuple(sorted({_normal_path(item) for item in paths}))

    diff_parts = [
        _run_git(
            worktree,
            ["diff", "--no-ext-diff", "--binary", "HEAD", "--"],
        ).stdout
    ]
    tracked = {
        line.split("\t", 2)[-1]
        for line in _run_git(
            worktree, ["diff", "--numstat", "HEAD", "--"]
        ).stdout.splitlines()
        if "\t" in line
    }
    diff_lines = 0
    for line in _run_git(
        worktree, ["diff", "--numstat", "HEAD", "--"]
    ).stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            diff_lines += int(parts[0]) + int(parts[1])
        elif len(parts) >= 2 and "-" in parts[:2]:
            # A binary patch has no meaningful line count and must never slip
            # beneath a small automatic-merge limit.
            diff_lines += 1_000_000_000
    for path in normalized:
        if path in tracked:
            continue
        source = worktree / path
        if not source.is_file():
            continue
        addition = _run_git(
            worktree,
            ["diff", "--no-index", "--", "/dev/null", path],
            accepted=frozenset({0, 1}),
        ).stdout
        diff_parts.append(addition)
        try:
            diff_lines += len(source.read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            diff_lines += 1_000_000_000
    return DiffInspection(
        changed_paths=normalized,
        diff_lines=diff_lines,
        diff="".join(diff_parts),
    )


@dataclass(frozen=True)
class Worktree:
    repo: Path
    path: Path
    branch: str

    def cleanup(self, *, delete_branch: bool) -> None:
        if self.path.exists():
            _run_git(
                self.repo,
                ["worktree", "remove", "--force", str(self.path)],
            )
        _run_git(self.repo, ["worktree", "prune"])
        if delete_branch:
            _run_git(
                self.repo,
                ["branch", "-D", "--", self.branch],
                accepted=frozenset({0, 1}),
            )


def create_worktree(repo: Path, path: Path, branch: str) -> Worktree:
    root = git_root(repo)
    if path.exists():
        raise GitError(f"worktree path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(root, ["check-ref-format", "--branch", branch])
    existing = _run_git(
        root,
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        accepted=frozenset({0, 1}),
    )
    if existing.returncode == 0:
        raise GitError(f"fix branch already exists: {branch}")
    _run_git(root, ["worktree", "add", "-b", branch, str(path), "HEAD"])
    return Worktree(repo=root, path=path, branch=branch)


def commit_fix(worktree: Worktree, finding_id: str, title: str) -> str:
    _run_git(worktree.path, ["add", "--all"])
    _run_git(
        worktree.path,
        [
            "commit",
            "-m",
            f"fix: {title}",
            "-m",
            f"Maida-Heal-Finding: {finding_id}",
        ],
    )
    return _run_git(worktree.path, ["rev-parse", "HEAD"]).stdout.strip()


def localize(
    repo: Path,
    *,
    onset: datetime,
    terms: list[str],
    limit: int = 5,
) -> list[LocalizationCandidate]:
    """Join drift onset to recent git paths with deliberately naive scoring."""
    if onset.tzinfo is None or onset.utcoffset() is None:
        raise ValueError("localization onset must include a timezone")
    since = (onset - timedelta(days=14)).astimezone(timezone.utc).isoformat()
    until = (onset + timedelta(days=1)).astimezone(timezone.utc).isoformat()
    delimiter = "__MAIDA_HEAL_COMMIT__"
    output = _run_git(
        repo,
        [
            "log",
            f"--since={since}",
            f"--until={until}",
            f"--format={delimiter}%H%x09%cI",
            "--name-only",
            "--",
        ],
    ).stdout
    tokens = {term.lower() for term in terms if len(term) >= 3}
    candidates: list[LocalizationCandidate] = []
    commit = ""
    committed_at: datetime | None = None
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(delimiter):
            commit, timestamp = line[len(delimiter) :].split("\t", 1)
            committed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            continue
        if not commit or committed_at is None:
            continue
        lowered = line.lower()
        overlap = sorted(token for token in tokens if token in lowered)
        recency = max(
            0.0, 1.0 - abs((onset - committed_at).total_seconds()) / 1_209_600
        )
        score = len(overlap) * 2.0 + recency
        excerpt = _run_git(
            repo,
            ["show", "--format=", "--unified=1", commit, "--", line],
        ).stdout[:8000]
        candidates.append(
            LocalizationCandidate(
                path=line,
                commit=commit,
                committed_at=committed_at,
                score=score,
                reason=(
                    f"path overlaps: {', '.join(overlap)}"
                    if overlap
                    else "changed near the naive drift onset"
                ),
                diff_excerpt=excerpt,
            )
        )
    candidates.sort(key=lambda item: (-item.score, item.path, item.commit))
    unique: list[LocalizationCandidate] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        key = (item.path, item.commit)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique[:limit]
