"""Opt-in bounded merge decisions and recurrence-triggered rollback."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from maida.heal.gitops import GitError, Worktree, create_worktree, resume_worktree
from maida.heal.killswitch import lock_blocks, sync_ci_pause_scope
from maida.heal.models import (
    AutoMergeConfig,
    EventEnvelope,
    EventType,
    Finding,
    FindingSource,
    FindingStatus,
    GateManifest,
    HealConfig,
    LoopMode,
    MergeRecord,
)
from maida.heal.state import (
    StateError,
    StateStore,
    load_gate_manifest,
    read_json,
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
        raise StateError("full mode requires gate and fixes configuration")
    if config.mode is not LoopMode.FULL or config.activation is None:
        raise StateError("auto-merge requires full mode with activation attestation")
    if config.auto_merge is not None:
        raise ValueError("auto-merge is already enabled")
    auto = AutoMergeConfig(
        enabled_at=now,
        max_diff_lines=max_diff_lines,
        daily_budget=daily_budget,
        recurrence_hours=recurrence_hours,
    )
    config.auto_merge = auto
    repo = config.fixes.local_repo()
    manifest = load_gate_manifest(repo)
    manifest = GateManifest.model_validate(
        {
            **manifest.model_dump(mode="json"),
            "mode": config.mode.value,
            "activation": config.activation.model_dump(mode="json"),
            "auto_merge": auto.model_dump(mode="json"),
        }
    )
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
    if finding.merge is not None:
        return ReleaseDecision(False, "merge is already recorded")
    if not finding.attempts or finding.attempts[-1].pull_request_number is None:
        return ReleaseDecision(False, "finding has no pull request")
    if finding.attempts[-1].diff_lines > auto.max_diff_lines:
        return ReleaseDecision(False, "diff exceeds the automatic merge limit")
    if (
        lock_blocks(repo / ".maida-heal" / "heal.lock", operation="auto_merge")
        or lock_blocks(repo / ".maida" / "heal.lock", operation="auto_merge")
        or os.environ.get("MAIDA_HEAL_PAUSED", "").lower()
        in {"true", "all", "fix_dispatch"}
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
    """Record verified PRs merged by customer automation or a person."""
    if config.fixes is None:
        return []
    selected = observer or GitHubHumanMergeObserver()
    repo = config.fixes.local_repo()
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
    def _existing_pr(self, *, repo: Path, branch: str) -> str | None:
        viewed = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "1",
                "--json",
                "url",
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if viewed.returncode != 0:
            raise ReleaseError("could not reconcile the recurrence revert PR")
        try:
            payload = json.loads(viewed.stdout)
        except json.JSONDecodeError as error:
            raise ReleaseError("gh returned invalid recurrence PR data") from error
        if not isinstance(payload, list) or not payload:
            return None
        item = payload[0]
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str):
            raise ReleaseError("gh returned invalid recurrence PR data")
        return url

    def _ready(self, worktree: Worktree, finding: Finding) -> bool:
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=worktree.path,
            text=True,
            capture_output=True,
            check=False,
        )
        message = subprocess.run(
            ["git", "log", "-1", "--format=%B"],
            cwd=worktree.path,
            text=True,
            capture_output=True,
            check=False,
        )
        trailer = f"Maida-Heal-Rollback: {finding.id}"
        return (
            status.returncode == 0
            and not status.stdout
            and message.returncode == 0
            and trailer in message.stdout.splitlines()
        )

    def open_revert(
        self,
        *,
        repo: Path,
        worktree_path: Path,
        branch: str,
        commit: str,
        finding: Finding,
    ) -> str:
        existing = self._existing_pr(repo=repo, branch=branch)
        if existing is not None:
            return existing
        resumed = False
        if worktree_path.exists():
            worktree = Worktree(repo=repo, path=worktree_path, branch=branch)
            resumed = True
        else:
            try:
                worktree = resume_worktree(repo, worktree_path, branch)
                resumed = True
            except GitError:
                worktree = create_worktree(repo, worktree_path, branch)
        try:
            if resumed and not self._ready(worktree, finding):
                worktree.cleanup(delete_branch=True)
                worktree = create_worktree(repo, worktree_path, branch)
                resumed = False
            if not resumed:
                reverted = subprocess.run(
                    ["git", "revert", "--no-edit", commit],
                    cwd=worktree.path,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if reverted.returncode != 0:
                    raise ReleaseError(
                        "automatic revert conflicted; repository is paused"
                    )
                marked = subprocess.run(
                    [
                        "git",
                        "commit",
                        "--amend",
                        "--no-edit",
                        "--trailer",
                        f"Maida-Heal-Rollback: {finding.id}",
                    ],
                    cwd=worktree.path,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if marked.returncode != 0:
                    raise ReleaseError("could not mark the recurrence revert commit")
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
                        f"Recurrence finding `{finding.id}` matched a merged fix for "
                        "the same metric and stream within the recurrence "
                        "window.\n\n"
                        "This revert is never auto-merged. Maida-heal is paused until "
                        "an operator runs `maida-heal resume`.\n"
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


def _emit_rollback_event(
    state: StateStore,
    config: HealConfig,
    finding: Finding,
    prior: Finding,
    *,
    now: datetime,
    url: str,
    branch: str,
) -> str:
    from maida.heal.events import EventJournal

    event = EventEnvelope.create(
        event_type=EventType.ROLLBACK_OPENED,
        occurred_at=now,
        dedupe_key=f"{finding.id}:rollback:{prior.id}:{url}",
        stream_id=finding.stream_id,
        finding_id=finding.id,
        data={
            "finding_id": finding.id,
            "prior_finding_id": prior.id,
            "pull_request_url": url,
            "branch": branch,
        },
    )
    journal = EventJournal(state.project_root, config.events)
    journal.queue(event)
    journal.flush()
    return event.event_id


def _emit_recurrence_pause_event(
    state: StateStore,
    config: HealConfig,
    finding: Finding,
    prior: Finding,
    *,
    now: datetime,
) -> str:
    from maida.heal.events import EventJournal

    event = EventEnvelope.create(
        event_type=EventType.LOOP_PAUSED,
        occurred_at=now,
        dedupe_key=f"{finding.id}:recurrence-pause:{prior.id}",
        stream_id=finding.stream_id,
        finding_id=finding.id,
        data={
            "actor": "system",
            "reason": f"recurrence after merged fix for {prior.id}",
            "scope": "fix_dispatch",
        },
    )
    journal = EventJournal(state.project_root, config.events)
    journal.queue(event)
    journal.flush()
    return event.event_id


def handle_recurrence(
    state: StateStore,
    config: HealConfig,
    finding: Finding,
    *,
    now: datetime,
    publisher: RollbackPublisher | None = None,
    pause_sync: Callable[[HealConfig], object] | None = None,
) -> RollbackDecision:
    if config.mode is not LoopMode.FULL or config.fixes is None:
        return RollbackDecision(False)
    stream = next(
        (item for item in config.streams if item.id == finding.stream_id), None
    )
    if stream is not None and (
        not stream.enabled or config.effective_mode(stream) is not LoopMode.FULL
    ):
        return RollbackDecision(False)
    recurrence_hours = (
        config.auto_merge.recurrence_hours if config.auto_merge is not None else 48
    )
    for prior in reversed(state.list_findings(config)):
        if (
            prior.id == finding.id
            or prior.stream_id != finding.stream_id
            or prior.merge is None
            or prior.status is not FindingStatus.CLOSED
            or not set(prior.metric_names).intersection(finding.metric_names)
        ):
            continue
        if finding.detected_at < prior.merge.merged_at:
            continue
        deadline = prior.merge.merged_at + timedelta(hours=recurrence_hours)
        if finding.detected_at > deadline or now < finding.detected_at:
            continue
        repo = config.fixes.local_repo()
        branch = f"maida-heal/revert-{finding.id}"
        selected = publisher or GitHubRollbackPublisher()
        finding.source = FindingSource.POST_MERGE_WATCH
        state.save_finding(finding, config)
        existing_lock: dict[str, object] = {}
        if state.lock_path.is_file():
            try:
                existing_lock = read_json(state.lock_path)
            except StateError:
                existing_lock = {}
        same_recurrence = (
            existing_lock.get("recurrence_finding_id") == finding.id
            and existing_lock.get("prior_finding_id") == prior.id
        )
        if (
            same_recurrence
            and existing_lock.get("ci_pause_synced") is True
            and isinstance(existing_lock.get("pause_event_id"), str)
            and isinstance(existing_lock.get("rollback_event_id"), str)
        ):
            return RollbackDecision(False)
        lock_payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "paused_at": now.isoformat(),
            "actor": "system",
            "reason": f"recurrence after merged fix for {prior.id}",
            "scope": "fix_dispatch",
            "recurrence_finding_id": finding.id,
            "prior_finding_id": prior.id,
            "revert_branch": branch,
        }
        if same_recurrence:
            lock_payload.update(existing_lock)
        write_kill_switch(
            state,
            config,
            lock_payload,
        )
        if lock_payload.get("ci_pause_synced") is not True:
            selected_sync = pause_sync or (
                lambda selected_config: sync_ci_pause_scope(
                    selected_config, scope="fix_dispatch"
                )
            )
            selected_sync(config)
            lock_payload["ci_pause_synced"] = True
            write_kill_switch(state, config, lock_payload)
        if not isinstance(lock_payload.get("pause_event_id"), str):
            lock_payload["pause_event_id"] = _emit_recurrence_pause_event(
                state,
                config,
                finding,
                prior,
                now=now,
            )
            write_kill_switch(state, config, lock_payload)
        stored_url = lock_payload.get("revert_pull_request")
        if isinstance(stored_url, str):
            url = stored_url
        else:
            url = selected.open_revert(
                repo=repo,
                worktree_path=state.root / "worktrees" / f"revert-{finding.id}",
                branch=branch,
                commit=prior.merge.commit,
                finding=finding,
            )
        lock_payload["revert_pull_request"] = url
        write_kill_switch(state, config, lock_payload)
        lock_payload["rollback_event_id"] = _emit_rollback_event(
            state,
            config,
            finding,
            prior,
            now=now,
            url=url,
            branch=branch,
        )
        write_kill_switch(state, config, lock_payload)
        return RollbackDecision(True, url, prior.id)
    return RollbackDecision(False)


def manifest_auto_merge(repo: Path) -> AutoMergeConfig | None:
    manifest: GateManifest = load_gate_manifest(repo)
    return manifest.auto_merge
