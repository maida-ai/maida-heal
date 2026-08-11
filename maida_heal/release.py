"""Tier-4 bounded release decisions and recurrence-triggered rollback."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from maida_heal.gitops import create_worktree
from maida_heal.models import (
    AutoMergeConfig,
    Finding,
    FindingSource,
    FindingStatus,
    GateManifest,
    HealConfig,
    MergeRecord,
)
from maida_heal.state import (
    StateError,
    StateStore,
    load_gate_manifest,
    save_gate_manifest,
    write_kill_switch,
)


class ReleaseError(RuntimeError):
    """A requested GitHub release or rollback operation failed."""


def enable_auto_merge(
    state: StateStore,
    config: HealConfig,
    *,
    now: datetime,
    max_diff_lines: int = 200,
    daily_budget: int = 3,
    recurrence_hours: int = 48,
) -> HealConfig:
    if config.gate is None or config.fixes is None:
        raise StateError("Enable the Maida-heal gate before enabling auto-merge")
    if config.auto_merge is not None:
        raise ValueError("auto-merge is already enabled")
    watched = [
        finding
        for finding in state.list_findings(config)
        if finding.status is FindingStatus.CLOSED
        and finding.merge is not None
        and finding.merge.mode == "human"
    ]
    if not watched:
        raise ValueError(
            "auto-merge requires at least one verified finding that a human merged"
        )
    auto = AutoMergeConfig(
        enabled_at=now,
        max_diff_lines=max_diff_lines,
        daily_budget=daily_budget,
        recurrence_hours=recurrence_hours,
    )
    config.auto_merge = auto
    repo = Path(config.fixes.repo_local_path).resolve()
    manifest = load_gate_manifest(repo)
    manifest.auto_merge = auto
    save_gate_manifest(repo, manifest)
    state.save_config(config)
    return config


@dataclass(frozen=True)
class MergeOutcome:
    commit: str


@dataclass(frozen=True)
class PullRequestInspection:
    diff_lines: int
    changed_paths: tuple[str, ...]


class Releaser(Protocol):
    def merges_today(self, *, repo: Path, day: str) -> int: ...

    def inspect(self, pull_request: int, *, repo: Path) -> PullRequestInspection: ...

    def merge(self, pull_request: int, *, repo: Path) -> MergeOutcome: ...


class GitHubReleaser:
    def merges_today(self, *, repo: Path, day: str) -> int:
        completed = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--search",
                f"merged:>={day}",
                "--limit",
                "100",
                "--json",
                "number,headRefName",
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise ReleaseError("could not read today's merged heal pull requests")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseError("gh returned invalid merge-budget JSON") from error
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise ReleaseError("gh returned an invalid merge-budget result")
        return sum(
            isinstance(item.get("headRefName"), str)
            and item["headRefName"].startswith("maida-heal/")
            and not item["headRefName"].startswith("maida-heal/revert-")
            for item in payload
        )

    def inspect(self, pull_request: int, *, repo: Path) -> PullRequestInspection:
        viewed = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pull_request),
                "--json",
                "additions,deletions,files",
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if viewed.returncode != 0:
            raise ReleaseError("could not inspect the current pull request diff")
        try:
            payload = json.loads(viewed.stdout)
            additions = payload["additions"]
            deletions = payload["deletions"]
            files = payload["files"]
            if (
                not isinstance(additions, int)
                or not isinstance(deletions, int)
                or not isinstance(files, list)
            ):
                raise TypeError
            paths = tuple(
                item["path"]
                for item in files
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            )
            if len(paths) != len(files):
                raise TypeError
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ReleaseError("gh returned invalid pull-request diff data") from error
        return PullRequestInspection(additions + deletions, paths)

    def merge(self, pull_request: int, *, repo: Path) -> MergeOutcome:
        merged = subprocess.run(
            ["gh", "pr", "merge", str(pull_request), "--squash"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if merged.returncode != 0:
            raise ReleaseError(
                "automatic merge failed; leaving the PR for human review"
            )
        viewed = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pull_request),
                "--json",
                "mergeCommit",
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            payload = json.loads(viewed.stdout)
            commit = payload["mergeCommit"]["oid"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ReleaseError("merged PR has no readable merge commit") from error
        if not isinstance(commit, str) or len(commit) < 7:
            raise ReleaseError("merged PR returned an invalid merge commit")
        return MergeOutcome(commit)


@dataclass(frozen=True)
class ReleaseDecision:
    merged: bool
    reason: str
    commit: str | None = None


def maybe_auto_merge(
    repo: Path,
    finding: Finding,
    auto: AutoMergeConfig,
    *,
    now: datetime,
    releaser: Releaser | None = None,
) -> ReleaseDecision:
    """Merge only inside the strict envelope; every refusal is human review."""
    if finding.status is not FindingStatus.CLOSED:
        return ReleaseDecision(False, "finding is not closed")
    if not finding.attempts or finding.attempts[-1].pull_request_number is None:
        return ReleaseDecision(False, "finding has no pull request")
    if finding.attempts[-1].diff_lines > auto.max_diff_lines:
        return ReleaseDecision(False, "diff exceeds the automatic merge limit")
    if (
        (repo / ".maida-heal" / "heal.lock").exists()
        or (repo / ".maida" / "heal.lock").exists()
        or os.environ.get("MAIDA_HEAL_PAUSED", "").lower() == "true"
    ):
        return ReleaseDecision(False, "kill switch is active")
    selected = releaser or GitHubReleaser()
    day = now.astimezone(timezone.utc).date().isoformat()
    try:
        if selected.merges_today(repo=repo, day=day) >= auto.daily_budget:
            return ReleaseDecision(False, "daily automatic merge budget is exhausted")
        pull_request = finding.attempts[-1].pull_request_number
        inspection = selected.inspect(pull_request, repo=repo)
        if inspection.diff_lines > auto.max_diff_lines:
            return ReleaseDecision(False, "current PR diff exceeds the merge limit")
        recorded_paths = set(finding.attempts[-1].changed_paths)
        if set(inspection.changed_paths) != recorded_paths:
            return ReleaseDecision(
                False, "current PR paths changed after fixer approval"
            )
        outcome = selected.merge(pull_request, repo=repo)
    except ReleaseError as error:
        return ReleaseDecision(False, str(error))
    finding.merge = MergeRecord(
        mode="automatic",
        merged_at=now,
        commit=outcome.commit,
        pull_request_number=pull_request,
    )
    return ReleaseDecision(
        True, "merged inside the configured envelope", outcome.commit
    )


class HumanMergeObserver(Protocol):
    def merged_record(self, finding: Finding, *, repo: Path) -> MergeRecord | None: ...


class GitHubHumanMergeObserver:
    """Read merged PR state without changing GitHub configuration or content."""

    def merged_record(self, finding: Finding, *, repo: Path) -> MergeRecord | None:
        if not finding.attempts:
            return None
        pull_request = finding.attempts[-1].pull_request_number
        if pull_request is None:
            return None
        viewed = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pull_request),
                "--json",
                "state,mergedAt,mergeCommit",
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if viewed.returncode != 0:
            raise ReleaseError("could not inspect the previously verified pull request")
        try:
            payload = json.loads(viewed.stdout)
            if payload.get("state") != "MERGED":
                return None
            merged_at = datetime.fromisoformat(
                str(payload["mergedAt"]).replace("Z", "+00:00")
            )
            commit = payload["mergeCommit"]["oid"]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ReleaseError(
                "gh returned invalid merged pull-request data"
            ) from error
        if not isinstance(commit, str) or len(commit) < 7:
            raise ReleaseError("merged pull request has no valid merge commit")
        return MergeRecord(
            mode="human",
            merged_at=merged_at,
            commit=commit,
            pull_request_number=pull_request,
        )


def refresh_human_merges(
    state: StateStore,
    config: HealConfig,
    *,
    observer: HumanMergeObserver | None = None,
) -> list[Finding]:
    """Record verified PRs that were merged by a person before tier 4."""
    if config.fixes is None:
        return []
    selected = observer or GitHubHumanMergeObserver()
    repo = Path(config.fixes.repo_local_path).resolve()
    recorded: list[Finding] = []
    for finding in state.list_findings(config):
        if finding.status is not FindingStatus.CLOSED or finding.merge is not None:
            continue
        merged = selected.merged_record(finding, repo=repo)
        if merged is None:
            continue
        finding.merge = merged
        state.save_finding(finding, config)
        recorded.append(finding)
    return recorded


def mark_human_merge(
    state: StateStore,
    config: HealConfig,
    finding_id: str,
    *,
    pull_request: int,
    commit: str,
    merged_at: datetime,
) -> Finding:
    finding = state.load_finding(finding_id, config)
    if finding.status is not FindingStatus.CLOSED:
        raise ValueError("only a verified closed finding can record a human merge")
    finding.merge = MergeRecord(
        mode="human",
        merged_at=merged_at,
        commit=commit,
        pull_request_number=pull_request,
    )
    state.save_finding(finding, config)
    return finding


class RollbackPublisher(Protocol):
    def open_revert(
        self,
        *,
        repo: Path,
        worktree_path: Path,
        branch: str,
        commit: str,
        finding: Finding,
    ) -> str: ...


class GitHubRollbackPublisher:
    def open_revert(
        self,
        *,
        repo: Path,
        worktree_path: Path,
        branch: str,
        commit: str,
        finding: Finding,
    ) -> str:
        paused = subprocess.run(
            [
                "gh",
                "variable",
                "set",
                "MAIDA_HEAL_PAUSED",
                "--body",
                "true",
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if paused.returncode != 0:
            raise ReleaseError(
                "local recurrence lock written, but the GitHub Actions kill "
                "switch could not be set"
            )
        worktree = create_worktree(repo, worktree_path, branch)
        try:
            reverted = subprocess.run(
                ["git", "revert", "--no-edit", commit],
                cwd=worktree.path,
                text=True,
                capture_output=True,
                check=False,
            )
            if reverted.returncode != 0:
                raise ReleaseError("automatic revert conflicted; repository is paused")
            pushed = subprocess.run(
                ["git", "push", "--set-upstream", "origin", branch],
                cwd=worktree.path,
                text=True,
                capture_output=True,
                check=False,
            )
            if pushed.returncode != 0:
                raise ReleaseError("could not push recurrence revert branch")
            descriptor, name = tempfile.mkstemp(
                prefix="maida-heal-revert-", suffix=".md"
            )
            body_path = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(
                        f"Recurrence finding `{finding.id}` matched an automatically "
                        "merged metric and stream within the configured recurrence "
                        "window.\n\n"
                        "This revert is never auto-merged. Maida-heal is paused until "
                        "a human runs `maida-heal resume`.\n"
                    )
                created = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "create",
                        "--head",
                        branch,
                        "--title",
                        f"revert: recurrence after {finding.id}",
                        "--body-file",
                        str(body_path),
                    ],
                    cwd=worktree.path,
                    text=True,
                    capture_output=True,
                    check=False,
                )
            finally:
                body_path.unlink(missing_ok=True)
            if created.returncode != 0:
                raise ReleaseError("could not open recurrence revert pull request")
            return created.stdout.strip().splitlines()[-1]
        finally:
            worktree.cleanup(delete_branch=False)


@dataclass(frozen=True)
class RollbackDecision:
    opened: bool
    pull_request_url: str | None = None
    prior_finding_id: str | None = None


def handle_recurrence(
    state: StateStore,
    config: HealConfig,
    finding: Finding,
    *,
    now: datetime,
    publisher: RollbackPublisher | None = None,
) -> RollbackDecision:
    if config.auto_merge is None or config.fixes is None:
        return RollbackDecision(False)
    for prior in reversed(state.list_findings(config)):
        if (
            prior.id == finding.id
            or prior.stream_id != finding.stream_id
            or prior.merge is None
            or prior.merge.mode != "automatic"
            or prior.status is not FindingStatus.CLOSED
            or not set(prior.metric_names).intersection(finding.metric_names)
        ):
            continue
        if finding.detected_at < prior.merge.merged_at:
            continue
        deadline = prior.merge.merged_at + timedelta(
            hours=config.auto_merge.recurrence_hours
        )
        if finding.detected_at > deadline or now < finding.detected_at:
            continue
        repo = Path(config.fixes.repo_local_path).resolve()
        branch = f"maida-heal/revert-{finding.id}"
        selected = publisher or GitHubRollbackPublisher()
        finding.source = FindingSource.POST_MERGE_WATCH
        state.save_finding(finding, config)
        lock_payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "paused_at": now.isoformat(),
            "actor": "system",
            "reason": f"recurrence after automatic merge for {prior.id}",
        }
        write_kill_switch(
            state,
            config,
            lock_payload,
        )
        url = selected.open_revert(
            repo=repo,
            worktree_path=state.root / "worktrees" / f"revert-{finding.id}",
            branch=branch,
            commit=prior.merge.commit,
            finding=finding,
        )
        lock_payload["revert_pull_request"] = url
        write_kill_switch(state, config, lock_payload)
        return RollbackDecision(True, url, prior.id)
    return RollbackDecision(False)


def manifest_auto_merge(repo: Path) -> AutoMergeConfig | None:
    manifest: GateManifest = load_gate_manifest(repo)
    return manifest.auto_merge
