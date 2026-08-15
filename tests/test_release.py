from __future__ import annotations

import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from maida.heal.models import (
    ActivationConfig,
    Actor,
    AutoMergeConfig,
    Finding,
    FindingSource,
    FindingStatus,
    FixAttempt,
    FixesConfig,
    GateConfig,
    GateManifest,
    HealConfig,
    HistoryEvent,
    LangfuseConfig,
    LoopMode,
    MergeRecord,
    MetricFailure,
    StreamConfig,
)
from maida.heal.release import (
    GitHubReleaser,
    MergeOutcome,
    PullRequestInspection,
    ReleaseError,
    enable_auto_merge,
    handle_recurrence,
    maybe_auto_merge,
)
from maida.heal.state import StateError, StateStore, save_gate_manifest

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def finding(
    identifier: str,
    *,
    status: FindingStatus = FindingStatus.CLOSED,
    diff_lines: int = 12,
    merge: MergeRecord | None = None,
    detected_at: datetime = NOW,
) -> Finding:
    return Finding(
        id=identifier,
        stream="support-agent",
        stream_id="support-agent-aabbccdd",
        source=FindingSource.SHADOW_WATCH,
        status=status,
        title="step_count drift on support-agent",
        summary="Structural step-count drift.",
        detected_at=detected_at,
        updated_at=detected_at,
        metric_failures=[
            MetricFailure(
                metric="step_count",
                kind="distributional",
                decision_rule="wilson_one_sided",
                run_ids=["a" * 32],
                evidence_pointer=".maida-heal/reports/report.json",
            )
        ],
        attempts=[
            FixAttempt(
                number=1,
                fixer="command",
                branch=f"maida-heal/{identifier}-a1",
                outcome="verified",
                changed_paths=["prompts/agent.md"],
                diff_lines=diff_lines,
                pull_request_number=17,
            )
        ],
        merge=merge,
        history=[
            HistoryEvent(
                timestamp=detected_at,
                actor=Actor.VERIFIER,
                action="finding_closed" if status is FindingStatus.CLOSED else "opened",
                detail="Fixture state.",
            )
        ],
    )


def configured(
    tmp_path: Path, *, with_gate: bool = True
) -> tuple[StateStore, HealConfig]:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    state = StateStore(tmp_path / "control")
    config = HealConfig(
        mode=LoopMode.VERIFY if with_gate else LoopMode.PROPOSE,
        langfuse=LangfuseConfig(
            host="https://example.test", credential_source="environment"
        ),
        fixes=FixesConfig(
            repo="maida-ai/example",
            repo_local_path=str(repo),
            fixer="command",
            command=["fixture-fixer"],
        ),
        gate=(
            GateConfig(
                command=["gate", "{report}"],
                holdout_command=["holdout", "{report}"],
            )
            if with_gate
            else None
        ),
    )
    state.save_config(config)
    if with_gate:
        save_gate_manifest(
            repo,
            GateManifest(
                command=["gate", "{report}"],
                holdout_command=["holdout", "{report}"],
                holdout_fraction=0.25,
            ),
        )
    return state, config


def authorize_full(config: HealConfig) -> None:
    config.activation = ActivationConfig(
        acknowledged_by="Operator <operator@example.test>",
        date=date(2026, 8, 11),
        statement="autonomous-fix-loop-authorized",
    )
    config.mode = LoopMode.FULL


def test_auto_merge_enable_requires_gate_and_full_mode_attestation(
    tmp_path: Path,
) -> None:
    state, no_gate = configured(tmp_path / "one", with_gate=False)
    with pytest.raises(StateError, match="gate"):
        enable_auto_merge(state, no_gate, now=NOW)

    state, config = configured(tmp_path / "two")
    with pytest.raises(StateError, match="activation attestation"):
        enable_auto_merge(state, config, now=NOW)

    authorize_full(config)
    state.save_config(config)
    enabled = enable_auto_merge(
        state,
        config,
        now=NOW,
        max_diff_lines=120,
        daily_budget=2,
    )
    assert enabled.mode is LoopMode.FULL
    assert enabled.auto_merge and enabled.auto_merge.max_diff_lines == 120


class FakeReleaser:
    def __init__(
        self,
        *,
        count: int = 0,
        fail: bool = False,
        current_diff: int = 12,
        current_paths: tuple[str, ...] = ("prompts/agent.md",),
    ) -> None:
        self.count = count
        self.fail = fail
        self.current_diff = current_diff
        self.current_paths = current_paths
        self.merged: list[int] = []

    def merges_today(self, *, repo: Path, day: str) -> int:
        del repo, day
        if self.fail:
            raise ReleaseError("GitHub unavailable; leave it for human review")
        return self.count

    def inspect(self, pull_request: int, *, repo: Path) -> PullRequestInspection:
        del pull_request, repo
        return PullRequestInspection(self.current_diff, self.current_paths)

    def merge(self, pull_request: int, *, repo: Path) -> MergeOutcome:
        del repo
        self.merged.append(pull_request)
        return MergeOutcome("abcdef1234567890")


@pytest.mark.parametrize(
    ("diff_lines", "count", "expected"),
    [(201, 0, "diff exceeds"), (20, 3, "budget is exhausted")],
)
def test_auto_merge_degrades_to_review_outside_limits(
    tmp_path: Path,
    diff_lines: int,
    count: int,
    expected: str,
) -> None:
    item = finding("mh-20260811-0123456789", diff_lines=diff_lines)
    decision = maybe_auto_merge(
        tmp_path,
        item,
        AutoMergeConfig(max_diff_lines=200, daily_budget=3),
        now=NOW,
        releaser=FakeReleaser(count=count),
    )
    assert decision.merged is False
    assert expected in decision.reason
    assert item.merge is None


def test_auto_merge_honors_kill_switch_and_delivery_failure(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    item = finding("mh-20260811-0123456789")
    lock = tmp_path / ".maida-heal" / "heal.lock"
    lock.parent.mkdir()
    lock.write_text("paused\n", encoding="utf-8")
    paused = maybe_auto_merge(
        tmp_path,
        item,
        AutoMergeConfig(),
        now=NOW,
        releaser=FakeReleaser(),
    )
    assert paused.reason == "kill switch is active"
    lock.unlink()
    monkeypatch.setenv("MAIDA_HEAL_PAUSED", "fix_dispatch")
    scoped = maybe_auto_merge(
        tmp_path,
        item,
        AutoMergeConfig(),
        now=NOW,
        releaser=FakeReleaser(),
    )
    assert scoped.reason == "kill switch is active"
    monkeypatch.delenv("MAIDA_HEAL_PAUSED")
    failed = maybe_auto_merge(
        tmp_path,
        item,
        AutoMergeConfig(),
        now=NOW,
        releaser=FakeReleaser(fail=True),
    )
    assert failed.merged is False
    assert "human review" in failed.reason


def test_auto_merge_records_the_merge_inside_every_bound(tmp_path: Path) -> None:
    item = finding("mh-20260811-0123456789")
    releaser = FakeReleaser()
    decision = maybe_auto_merge(
        tmp_path,
        item,
        AutoMergeConfig(),
        now=NOW,
        releaser=releaser,
    )
    assert decision.merged is True
    assert releaser.merged == [17]
    assert item.merge and item.merge.mode == "automatic"

    repeated = maybe_auto_merge(
        tmp_path,
        item,
        AutoMergeConfig(),
        now=NOW,
        releaser=releaser,
    )
    assert repeated.merged is False
    assert repeated.reason == "merge is already recorded"
    assert releaser.merged == [17]


@pytest.mark.parametrize(
    ("releaser", "reason"),
    [
        (FakeReleaser(current_diff=201), "current PR diff exceeds"),
        (
            FakeReleaser(current_paths=("prompts/agent.md", ".maida/policy.yaml")),
            "paths changed",
        ),
    ],
)
def test_auto_merge_rechecks_current_pr_after_the_fixer_exits(
    tmp_path: Path, releaser: FakeReleaser, reason: str
) -> None:
    item = finding("mh-20260811-0123456789")

    decision = maybe_auto_merge(
        tmp_path,
        item,
        AutoMergeConfig(),
        now=NOW,
        releaser=releaser,
    )

    assert decision.merged is False
    assert reason in decision.reason


def test_github_daily_budget_counts_only_heal_not_revert_branches(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    payload = (
        '[{"number": 1, "headRefName": "maida-heal/mh-one-a1"},'
        '{"number": 2, "headRefName": "feature/manual"},'
        '{"number": 3, "headRefName": "maida-heal/revert-mh-one"}]'
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, payload, "")

    monkeypatch.setattr("maida.heal.release.subprocess.run", fake_run)

    assert GitHubReleaser().merges_today(repo=tmp_path, day="2026-08-11") == 1


class FakeRollbackPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def open_revert(
        self,
        *,
        repo: Path,
        worktree_path: Path,
        branch: str,
        commit: str,
        finding: Finding,
    ) -> str:
        del repo, worktree_path, branch, finding
        self.calls.append(commit)
        if self.fail:
            raise ReleaseError("revert conflict")
        return "https://github.com/maida-ai/example/pull/99"


def test_handoff_recurrence_opens_revert_event_and_pauses_fix_dispatch(
    tmp_path: Path,
) -> None:
    state, config = configured(tmp_path)
    authorize_full(config)
    state.save_config(config)
    prior = finding(
        "mh-20260810-0123456789",
        detected_at=NOW - timedelta(hours=3),
        merge=MergeRecord(
            mode="human",
            merged_at=NOW - timedelta(hours=2),
            commit="deadbee123456789",
            pull_request_number=16,
        ),
    )
    current = finding(
        "mh-20260811-fedcba9876",
        status=FindingStatus.OPEN,
        detected_at=NOW,
    )
    current.attempts = []
    state.save_finding(prior, config)
    state.save_finding(current, config)
    publisher = FakeRollbackPublisher()
    pause_syncs: list[str] = []

    decision = handle_recurrence(
        state,
        config,
        current,
        now=NOW,
        publisher=publisher,
        pause_sync=lambda _config: pause_syncs.append("fix_dispatch"),
    )

    assert decision.opened is True
    assert publisher.calls == ["deadbee123456789"]
    assert state.lock_path.is_file()
    lock = state.lock_path.read_text()
    assert '"scope": "fix_dispatch"' in lock
    assert config.fixes is not None
    assert (config.fixes.local_repo() / ".maida" / "heal.lock").is_file()
    assert state.load_finding(current.id, config).status is FindingStatus.OPEN
    events = (state.root / "events.jsonl").read_text()
    assert '"type":"loop.paused"' in events
    assert '"type":"rollback.opened"' in events
    assert pause_syncs == ["fix_dispatch"]

    repeated = handle_recurrence(
        state,
        config,
        current,
        now=NOW,
        publisher=publisher,
        pause_sync=lambda _config: pause_syncs.append("fix_dispatch"),
    )
    assert repeated.opened is False
    assert publisher.calls == ["deadbee123456789"]
    assert pause_syncs == ["fix_dispatch"]
    assert events == (state.root / "events.jsonl").read_text()


def test_verify_only_stream_override_cannot_dispatch_recurrence_rollback(
    tmp_path: Path,
) -> None:
    state, config = configured(tmp_path)
    authorize_full(config)
    config.streams = [
        StreamConfig(
            id="support-agent-aabbccdd",
            name="support-agent",
            grouping="trace_name",
            grouping_key="traceName",
            grouping_value_hash="aabbccddeeff",
            trace_names=["support-agent"],
            mode=LoopMode.VERIFY,
        )
    ]
    state.save_config(config)
    prior = finding(
        "mh-20260810-0123456789",
        detected_at=NOW - timedelta(hours=3),
        merge=MergeRecord(
            mode="human",
            merged_at=NOW - timedelta(hours=2),
            commit="deadbee123456789",
            pull_request_number=16,
        ),
    )
    current = finding(
        "mh-20260811-fedcba9876",
        status=FindingStatus.OPEN,
        detected_at=NOW,
    )
    current.attempts = []
    state.save_finding(prior, config)
    state.save_finding(current, config)
    publisher = FakeRollbackPublisher()
    pause_syncs: list[str] = []

    decision = handle_recurrence(
        state,
        config,
        current,
        now=NOW,
        publisher=publisher,
        pause_sync=lambda _config: pause_syncs.append("fix_dispatch"),
    )

    assert decision.opened is False
    assert publisher.calls == []
    assert pause_syncs == []
    assert not state.lock_path.exists()


def test_recurrence_pauses_even_if_revert_creation_fails(tmp_path: Path) -> None:
    state, config = configured(tmp_path)
    authorize_full(config)
    config.auto_merge = AutoMergeConfig(enabled_at=NOW - timedelta(hours=2))
    state.save_config(config)
    prior = finding(
        "mh-20260810-0123456789",
        detected_at=NOW - timedelta(hours=3),
        merge=MergeRecord(
            mode="automatic",
            merged_at=NOW - timedelta(hours=2),
            commit="deadbee123456789",
            pull_request_number=16,
        ),
    )
    current = finding(
        "mh-20260811-fedcba9876",
        status=FindingStatus.OPEN,
        detected_at=NOW,
    )
    current.attempts = []
    state.save_finding(prior, config)
    state.save_finding(current, config)

    with pytest.raises(ReleaseError, match="revert conflict"):
        handle_recurrence(
            state,
            config,
            current,
            now=NOW,
            publisher=FakeRollbackPublisher(fail=True),
            pause_sync=lambda _config: None,
        )
    assert state.lock_path.is_file()
    assert config.fixes is not None
    assert (config.fixes.local_repo() / ".maida" / "heal.lock").is_file()

    recovered = FakeRollbackPublisher()
    decision = handle_recurrence(
        state,
        config,
        current,
        now=NOW,
        publisher=recovered,
        pause_sync=lambda _config: None,
    )
    assert decision.opened is True
    assert recovered.calls == ["deadbee123456789"]
    assert '"type":"rollback.opened"' in (state.root / "events.jsonl").read_text()
