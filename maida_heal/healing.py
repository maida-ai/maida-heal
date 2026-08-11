"""Tier-2 fix proposal orchestration with independent post-hoc enforcement."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import yaml

from maida_heal.fixers import Fixer, fixer_from_config
from maida_heal.gitops import (
    DiffInspection,
    Worktree,
    changed_symlink_violation,
    commit_fix,
    create_worktree,
    inspect_diff,
    localize,
    validate_changed_paths,
    writable_symlinks,
)
from maida_heal.models import (
    Actor,
    Finding,
    FindingStatus,
    FixAttempt,
    HealConfig,
    jsonable,
)
from maida_heal.state import StateError, StateStore


class PublishError(RuntimeError):
    """A branch could not be pushed or opened as a pull request."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str


class Publisher(Protocol):
    def publish(
        self,
        worktree: Worktree,
        finding: Finding,
        *,
        repository: str,
        body: str,
    ) -> PullRequest: ...


class GitHubPublisher:
    """Publish through the user's existing git remote and `gh` authentication."""

    def publish(
        self,
        worktree: Worktree,
        finding: Finding,
        *,
        repository: str,
        body: str,
    ) -> PullRequest:
        pushed = subprocess.run(
            ["git", "push", "--set-upstream", "origin", worktree.branch],
            cwd=worktree.path,
            text=True,
            capture_output=True,
            check=False,
        )
        if pushed.returncode != 0:
            raise PublishError(
                f"git push failed: {pushed.stderr.strip() or 'no diagnostic'}"
            )
        descriptor, body_name = tempfile.mkstemp(prefix="maida-heal-pr-", suffix=".md")
        body_path = Path(body_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(body)
            created = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    repository,
                    "--head",
                    worktree.branch,
                    "--title",
                    f"maida-heal: {finding.title}",
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
            raise PublishError(
                f"gh pr create failed: {created.stderr.strip() or 'no diagnostic'}"
            )
        url = created.stdout.strip().splitlines()[-1]
        try:
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError as error:
            raise PublishError(
                f"gh returned an invalid pull request URL: {url}"
            ) from error
        return PullRequest(number=number, url=url)


@dataclass(frozen=True)
class FixResult:
    finding_id: str
    attempt: int
    outcome: str
    branch: str
    inspection: DiffInspection
    pull_request: PullRequest | None = None
    rejection: str | None = None


def _policy_metrics(state: StateStore, finding: Finding) -> dict[str, object]:
    metrics: dict[str, object] = {}
    stream_dir = state.streams_dir / finding.stream_id
    for path in sorted(stream_dir.glob("*/policy.yaml")):
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        available = payload.get("metrics") if isinstance(payload, dict) else None
        if not isinstance(available, dict):
            continue
        for name in finding.metric_names:
            if name in available:
                metrics[name] = available[name]
    return metrics


def fixer_prompt(
    state: StateStore,
    config: HealConfig,
    finding: Finding,
    *,
    localization: list[object] | None = None,
) -> str:
    if config.fixes is None:
        raise ValueError("fixes are not enabled")
    structural = json.dumps(jsonable(finding), ensure_ascii=False, indent=2)
    metrics = yaml.safe_dump(_policy_metrics(state, finding), sort_keys=False).strip()
    candidates = localization if localization is not None else finding.localization
    location_text = json.dumps(
        [
            item.model_dump(mode="json")
            for item in candidates
            if hasattr(item, "model_dump")
        ],
        ensure_ascii=False,
        indent=2,
    )
    allowed = "\n".join(f"- {item}" for item in config.fixes.allowed_paths)
    return f"""You are the replaceable fix writer in an experimental self-healing loop.

Your only job is to edit the candidate worktree. You never verify or judge the fix.
Maida will make every behavioral decision after your work is complete.

Finding (structural evidence only; run IDs are pointers, never payloads):
{structural}

Metric definitions:
{metrics or "{}"}

Naive localization candidates and code diffs:
{location_text}

Writable path allowlist:
{allowed}

Hard instructions:
- Fix the cause named by the finding with the smallest maintainable change.
- Never edit .maida/**, .maida-heal/**, .github/workflows/**, or holdouts.
- Never weaken policy, baselines, scenarios, tests, or verification.
- Do not add secrets, trace payloads, or customer data.
- Do not commit, push, open a pull request, or run a verifier.
- Gate gaming is a rejection even if an aggregate report would turn green.

Edit files now and then exit. A separate post-hoc diff check is authoritative.
"""


def _fixer_environment(finding_id: str) -> dict[str, str]:
    allowed = {
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["MAIDA_HEAL_FINDING_ID"] = finding_id
    return env


def _pr_body(finding: Finding, *, gate_enabled: bool) -> str:
    metric_lines = "\n".join(f"- `{name}`" for name in finding.metric_names)
    verification = (
        "Maida behavioral verification is required in CI. Closure requires the "
        "specific metrics, holdouts, and no new failures."
        if gate_enabled
        else "verification: gate not enabled -- review manually"
    )
    return f"""## Finding

`{finding.id}` — {finding.title}

Affected structural metrics:
{metric_lines}

This pull request contains no production trace payloads. Finding evidence uses run
IDs and versioned Maida report pointers only.

## Verification

{verification}

The fix writer proposed this patch. It did not judge the result; Maida is the only
behavioral verifier.
"""


def _transition_to_proposed(finding: Finding, *, now: datetime, attempt: int) -> None:
    finding.transition(
        FindingStatus.FIX_PROPOSED,
        actor=Actor.FIXER,
        action="fix_proposed",
        detail=f"Fix writer completed attempt {attempt}; post-hoc checks started.",
        at=now,
    )


def _reject(
    state: StateStore,
    config: HealConfig,
    finding: Finding,
    worktree: Worktree,
    inspection: DiffInspection,
    *,
    now: datetime,
    attempt_number: int,
    reason: str,
) -> FixResult:
    if config.fixes is None:
        raise ValueError("fixes are not enabled")
    attempt = FixAttempt(
        number=attempt_number,
        fixer=config.fixes.fixer,
        branch=worktree.branch,
        started_at=now,
        finished_at=now,
        outcome="rejected",
        changed_paths=list(inspection.changed_paths),
        diff_lines=inspection.diff_lines,
    )
    finding.attempts.append(attempt)
    finding.cooldown_until = now + timedelta(hours=config.fixes.cooldown_hours)
    finding.transition(
        FindingStatus.FIX_REJECTED,
        actor=Actor.SYSTEM,
        action="fix_rejected",
        detail=reason,
        at=now,
    )
    state.save_finding(finding, config)
    worktree.cleanup(delete_branch=True)
    return FixResult(
        finding_id=finding.id,
        attempt=attempt_number,
        outcome="rejected",
        branch=worktree.branch,
        inspection=inspection,
        rejection=reason,
    )


def propose_fix(
    state: StateStore,
    config: HealConfig,
    finding_id: str,
    *,
    now: datetime,
    dry_run: bool,
    fixer: Fixer | None = None,
    publisher: Publisher | None = None,
) -> FixResult:
    if config.fixes is None:
        raise StateError("Fixes are not enabled. Run `maida-heal enable fixes` first.")
    if state.lock_path.exists():
        raise StateError("Maida-heal is paused; fixes are disabled by the kill switch")
    finding = state.load_finding(finding_id, config)
    if finding.status not in {FindingStatus.OPEN, FindingStatus.FIX_REJECTED}:
        raise ValueError(
            f"finding {finding.id} cannot be fixed from {finding.status.value}"
        )
    if finding.cooldown_until is not None and now < finding.cooldown_until:
        raise ValueError(
            f"finding cooldown is active until {finding.cooldown_until.isoformat()}"
        )
    attempt_number = len(finding.attempts) + 1
    if attempt_number > config.fixes.max_attempts_per_finding:
        raise ValueError(
            f"finding exhausted {config.fixes.max_attempts_per_finding} fix attempts"
        )
    repo = Path(config.fixes.repo_local_path).expanduser().resolve()
    localization = finding.localization or localize(
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
    branch = f"maida-heal/{finding.id}-a{attempt_number}"
    worktree = create_worktree(
        repo,
        state.root / "worktrees" / f"{finding.id}-a{attempt_number}",
        branch,
    )
    hazards = writable_symlinks(worktree.path, config.fixes.allowed_paths)
    if hazards:
        worktree.cleanup(delete_branch=True)
        raise ValueError(
            "writable paths contain symbolic links; remove or exclude them before "
            f"running a fixer: {', '.join(hazards)}"
        )
    selected_fixer = fixer or fixer_from_config(
        config.fixes.fixer, config.fixes.command
    )
    prompt = fixer_prompt(state, config, finding, localization=list(localization))
    try:
        selected_fixer.write(
            worktree.path,
            prompt,
            environment=_fixer_environment(finding.id),
        )
        inspection = inspect_diff(worktree.path)
    except Exception:
        worktree.cleanup(delete_branch=True)
        raise
    if not inspection.changed_paths:
        if dry_run:
            worktree.cleanup(delete_branch=True)
            return FixResult(
                finding.id,
                attempt_number,
                "dry_run",
                branch,
                inspection,
                rejection="fixer produced no changes",
            )
        _transition_to_proposed(finding, now=now, attempt=attempt_number)
        return _reject(
            state,
            config,
            finding,
            worktree,
            inspection,
            now=now,
            attempt_number=attempt_number,
            reason="fixer produced no changes",
        )
    violation = validate_changed_paths(
        inspection.changed_paths, config.fixes.allowed_paths
    )
    violation = violation or changed_symlink_violation(
        worktree.path, inspection.changed_paths
    )
    if dry_run:
        worktree.cleanup(delete_branch=True)
        return FixResult(
            finding.id,
            attempt_number,
            "dry_run",
            branch,
            inspection,
            rejection=(
                f"{violation.reason}: {', '.join(violation.offending_paths)}"
                if violation
                else None
            ),
        )

    finding.localization = list(localization)
    _transition_to_proposed(finding, now=now, attempt=attempt_number)
    if violation:
        return _reject(
            state,
            config,
            finding,
            worktree,
            inspection,
            now=now,
            attempt_number=attempt_number,
            reason=f"{violation.reason}: {', '.join(violation.offending_paths)}",
        )
    commit_fix(worktree, finding.id, finding.title)
    published = (publisher or GitHubPublisher()).publish(
        worktree,
        finding,
        repository=config.fixes.repo,
        body=_pr_body(finding, gate_enabled=config.gate is not None),
    )
    attempt = FixAttempt(
        number=attempt_number,
        fixer=config.fixes.fixer,
        branch=branch,
        started_at=now,
        finished_at=now,
        outcome="proposed",
        changed_paths=list(inspection.changed_paths),
        diff_lines=inspection.diff_lines,
        pull_request_number=published.number,
        pull_request_url=published.url,
    )
    finding.attempts.append(attempt)
    finding.cooldown_until = None
    state.save_finding(finding, config)
    worktree.cleanup(delete_branch=False)
    return FixResult(
        finding.id,
        attempt_number,
        "proposed",
        branch,
        inspection,
        pull_request=published,
    )
