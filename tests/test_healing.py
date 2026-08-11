import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import pytest

from maida_heal.fixers import Fixer
from maida_heal.healing import Publisher, PullRequest, propose_fix
from maida_heal.models import (
    Actor,
    Finding,
    FindingSource,
    FindingStatus,
    FixesConfig,
    HealConfig,
    HistoryEvent,
    LangfuseConfig,
    MetricFailure,
)
from maida_heal.state import StateStore

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    )
    return completed.stdout.strip()


def make_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Maida Heal Tests")
    git(path, "config", "user.email", "maida-heal@example.test")
    (path / "prompts").mkdir()
    (path / "prompts" / "agent.md").write_text("retries: 3\n", encoding="utf-8")
    (path / ".maida").mkdir()
    (path / ".maida" / "policy.yaml").write_text("version: 2\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial behavior")
    return path


def make_finding(state: StateStore, config: HealConfig) -> Finding:
    finding = Finding(
        id="mh-20260811-0123456789",
        stream="support-agent",
        stream_id="support-agent-aabbccdd",
        source=FindingSource.SHADOW_WATCH,
        title="step_count drift on support-agent",
        summary="A structural step-count regression.",
        detected_at=NOW,
        updated_at=NOW,
        metric_failures=[
            MetricFailure(
                metric="step_count",
                kind="distributional",
                decision_rule="wilson_one_sided",
                run_ids=["a" * 32],
                evidence_pointer=".maida-heal/reports/report.json",
                observed=8,
                prediction_bound=3,
            )
        ],
        history=[
            HistoryEvent(
                timestamp=NOW,
                actor=Actor.SYSTEM,
                action="finding_opened",
                detail="Detected drift.",
            )
        ],
    )
    state.save_finding(finding, config)
    return finding


def make_config(
    repo: Path,
    *,
    attempts: int = 2,
    cooldown_hours: int = 0,
) -> HealConfig:
    return HealConfig(
        langfuse=LangfuseConfig(
            host="https://example.test", credential_source="environment"
        ),
        fixes=FixesConfig(
            repo="maida-ai/example-agent",
            repo_local_path=str(repo),
            fixer="command",
            command=["fixture-fixer"],
            max_attempts_per_finding=attempts,
            cooldown_hours=cooldown_hours,
        ),
    )


class EditingFixer(Fixer):
    kind = "command"

    def __init__(self, path: str, content: str) -> None:
        self.path = path
        self.content = content

    def write(
        self, worktree: Path, prompt: str, *, environment: Mapping[str, str]
    ) -> None:
        assert "LANGFUSE_SECRET_KEY" not in environment
        assert "run IDs are pointers" in prompt
        destination = worktree / self.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.content, encoding="utf-8")


class SymlinkFixer(Fixer):
    kind = "command"

    def write(
        self, worktree: Path, prompt: str, *, environment: Mapping[str, str]
    ) -> None:
        del prompt, environment
        (worktree / "prompts" / "escape.md").symlink_to("/tmp/not-allowed")


class RenameProtectedFixer(Fixer):
    kind = "command"

    def write(
        self, worktree: Path, prompt: str, *, environment: Mapping[str, str]
    ) -> None:
        del prompt, environment
        (worktree / ".maida" / "policy.yaml").rename(
            worktree / "prompts" / "renamed-policy.md"
        )


class FakePublisher(Publisher):
    def __init__(self) -> None:
        self.bodies: list[str] = []

    def publish(
        self,
        worktree: object,
        finding: Finding,
        *,
        repository: str,
        body: str,
    ) -> PullRequest:
        del worktree, finding
        assert repository == "maida-ai/example-agent"
        self.bodies.append(body)
        return PullRequest(17, "https://github.com/maida-ai/example-agent/pull/17")


def test_protected_path_is_rejected_post_hoc_and_branch_is_deleted(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    state = StateStore(tmp_path / "control")
    config = make_config(repo)
    state.save_config(config)
    finding = make_finding(state, config)

    result = propose_fix(
        state,
        config,
        finding.id,
        now=NOW,
        dry_run=False,
        fixer=EditingFixer(".maida/policy.yaml", "version: 1\n"),
        publisher=FakePublisher(),
    )

    assert result.outcome == "rejected"
    assert result.rejection and "protected paths" in result.rejection
    assert git(repo, "branch", "--list", result.branch) == ""
    assert (repo / ".maida" / "policy.yaml").read_text() == "version: 2\n"
    stored = state.load_finding(finding.id, config)
    assert stored.status is FindingStatus.FIX_REJECTED
    assert stored.attempts[0].changed_paths == [".maida/policy.yaml"]


def test_allowed_patch_is_committed_with_trailer_and_opens_pr(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    state = StateStore(tmp_path / "control")
    config = make_config(repo)
    state.save_config(config)
    finding = make_finding(state, config)
    publisher = FakePublisher()

    result = propose_fix(
        state,
        config,
        finding.id,
        now=NOW,
        dry_run=False,
        fixer=EditingFixer("prompts/agent.md", "retries: 1\n"),
        publisher=publisher,
    )

    assert result.outcome == "proposed"
    assert result.pull_request and result.pull_request.number == 17
    message = git(repo, "log", "-1", "--format=%B", result.branch)
    assert "Maida-Heal-Finding: mh-20260811-0123456789" in message
    assert "verification: gate not enabled -- review manually" in publisher.bodies[0]
    assert (repo / "prompts" / "agent.md").read_text() == "retries: 3\n"
    stored = state.load_finding(finding.id, config)
    assert stored.status is FindingStatus.FIX_PROPOSED
    assert stored.attempts[0].pull_request_number == 17


def test_dry_run_leaves_finding_and_branches_unchanged(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    state = StateStore(tmp_path / "control")
    config = make_config(repo)
    state.save_config(config)
    finding = make_finding(state, config)

    result = propose_fix(
        state,
        config,
        finding.id,
        now=NOW,
        dry_run=True,
        fixer=EditingFixer("prompts/agent.md", "retries: 1\n"),
    )

    assert result.outcome == "dry_run"
    assert "retries: 1" in result.inspection.diff
    assert git(repo, "branch", "--list", result.branch) == ""
    stored = state.load_finding(finding.id, config)
    assert stored.status is FindingStatus.OPEN
    assert stored.attempts == []


def test_nested_untracked_files_are_counted_individually_for_merge_limits(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    state = StateStore(tmp_path / "control")
    config = make_config(repo)
    state.save_config(config)
    finding = make_finding(state, config)

    result = propose_fix(
        state,
        config,
        finding.id,
        now=NOW,
        dry_run=True,
        fixer=EditingFixer("prompts/new/nested.md", "one\ntwo\n"),
    )

    assert result.inspection.changed_paths == ("prompts/new/nested.md",)
    assert result.inspection.diff_lines == 2


def test_attempt_limit_refuses_before_creating_a_worktree(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    state = StateStore(tmp_path / "control")
    config = make_config(repo, attempts=1)
    state.save_config(config)
    finding = make_finding(state, config)
    rejected = propose_fix(
        state,
        config,
        finding.id,
        now=NOW,
        dry_run=False,
        fixer=EditingFixer(".maida/policy.yaml", "version: 1\n"),
        publisher=FakePublisher(),
    )
    assert rejected.outcome == "rejected"

    with pytest.raises(ValueError, match="exhausted 1 fix attempts"):
        propose_fix(
            state,
            config,
            finding.id,
            now=NOW,
            dry_run=True,
            fixer=EditingFixer("prompts/agent.md", "retries: 1\n"),
        )


def test_cooldown_refuses_a_second_attempt_before_creating_worktree(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    state = StateStore(tmp_path / "control")
    config = make_config(repo, cooldown_hours=24)
    state.save_config(config)
    finding = make_finding(state, config)
    propose_fix(
        state,
        config,
        finding.id,
        now=NOW,
        dry_run=False,
        fixer=EditingFixer(".maida/policy.yaml", "version: 1\n"),
        publisher=FakePublisher(),
    )

    with pytest.raises(ValueError, match="cooldown is active"):
        propose_fix(
            state,
            config,
            finding.id,
            now=NOW.replace(hour=13),
            dry_run=True,
            fixer=EditingFixer("prompts/agent.md", "retries: 1\n"),
        )


def test_new_symbolic_link_is_rejected_by_post_hoc_boundary(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "repo")
    state = StateStore(tmp_path / "control")
    config = make_config(repo)
    state.save_config(config)
    finding = make_finding(state, config)

    result = propose_fix(
        state,
        config,
        finding.id,
        now=NOW,
        dry_run=False,
        fixer=SymlinkFixer(),
        publisher=FakePublisher(),
    )

    assert result.outcome == "rejected"
    assert result.rejection and "symbolic links" in result.rejection


def test_renaming_a_protected_file_into_allowlist_is_still_rejected(
    tmp_path: Path,
) -> None:
    repo = make_repo(tmp_path / "repo")
    state = StateStore(tmp_path / "control")
    config = make_config(repo)
    state.save_config(config)
    finding = make_finding(state, config)

    result = propose_fix(
        state,
        config,
        finding.id,
        now=NOW,
        dry_run=False,
        fixer=RenameProtectedFixer(),
        publisher=FakePublisher(),
    )

    assert result.outcome == "rejected"
    assert result.rejection and "protected paths" in result.rejection
    assert set(result.inspection.changed_paths) == {
        ".maida/policy.yaml",
        "prompts/renamed-policy.md",
    }
