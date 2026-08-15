import json
import subprocess
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

from maida.heal.cli import app
from maida.heal.core import MaidaCLI, ReportCompatibilityError
from maida.heal.gate import (
    ClosureRunner,
    GateError,
    VerificationNotEnabled,
    enable_gate,
    verify_closure,
)
from maida.heal.models import (
    ActivationConfig,
    Actor,
    AutoMergeConfig,
    EventConfig,
    FindingStatus,
    FixAttempt,
    FixesConfig,
    LoopMode,
    WebhookSinkConfig,
)
from maida.heal.onboarding import (
    apply_stream_edits,
    attach,
    fixture_attachment_client,
)
from maida.heal.state import (
    StateStore,
    load_gate_manifest,
    save_gate_manifest,
    write_json,
)

NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def config_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Gate Tests")
    git(path, "config", "user.email", "gate@example.test")
    (path / "agent.py").write_text("print('fixture')\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def scaffold(tmp_path: Path) -> tuple[StateStore, Path, str]:
    control = tmp_path / "control"
    control.mkdir()
    state = StateStore(control)
    batch, client = fixture_attachment_client()
    attach(
        state,
        MaidaCLI(state),
        client,
        host="fixture://langfuse",
        credential_source="fixture",
        now=NOW,
        metadata_keys=["agent_id"],
        configure=lambda candidates: apply_stream_edits(candidates, select_all=True),
        progress=lambda _line: None,
        fixture_batch=batch,
    )
    repo = config_repo(tmp_path / "repo")
    config = state.load_config()
    config.fixes = FixesConfig(
        repo="maida-ai/example",
        repo_local_path=str(repo),
        fixer="command",
        command=["fixture-fixer"],
    )
    state.save_config(config)
    finding_id = next(
        item.id
        for item in state.list_findings(config)
        if "step_count" in item.metric_names
    )

    result = enable_gate(
        state,
        config,
        MaidaCLI(state),
        command=["fixture-gate", "{report}", "{suite}"],
        holdout_command=["fixture-holdout", "{report}", "{suite}"],
        now=NOW,
    )

    assert result.holdout_runs == 4
    assert result.training_runs == 10
    assert (repo / ".github" / "workflows" / "maida-heal.yml").is_file()
    workflow = (repo / ".github" / "workflows" / "maida-heal.yml").read_text(
        encoding="utf-8"
    )
    assert "!startsWith(github.head_ref, 'maida-heal/revert-')" in workflow
    assert not list(state.local_findings_dir.glob("mh-*.json"))
    return state, repo, finding_id


def report(
    *,
    step_verdict: str | None = "pass",
    verdict: str = "pass",
    extra_fail: bool = False,
    version: str = "2.0.0",
) -> dict[str, object]:
    aggregates: list[dict[str, object]] = []
    if step_verdict is not None:
        aggregates.append(
            {
                "check_name": "step_count",
                "kind": "distributional",
                "verdict": step_verdict,
            }
        )
    if extra_fail:
        aggregates.append(
            {
                "check_name": "latency_ms",
                "kind": "distributional",
                "verdict": "fail",
            }
        )
    return {
        "report_version": version,
        "verdict": verdict,
        "aggregate_results": aggregates,
    }


class ReportRunner(ClosureRunner):
    def __init__(
        self, candidate: dict[str, object], holdout: dict[str, object]
    ) -> None:
        self.candidate = candidate
        self.holdout = holdout

    def run(self, command: Sequence[str], *, cwd: Path) -> int:
        del cwd
        destination = Path(command[1])
        write_json(
            destination,
            self.holdout if command[2] == "holdout" else self.candidate,
        )
        selected = self.holdout if command[2] == "holdout" else self.candidate
        return 1 if selected.get("verdict") == "fail" else 0


class NoReportRunner(ClosureRunner):
    def run(self, command: Sequence[str], *, cwd: Path) -> int:
        del command, cwd
        return 0


class ExplodingRunner(ClosureRunner):
    def run(self, command: Sequence[str], *, cwd: Path) -> int:
        del command, cwd
        raise AssertionError("a persisted closure must not rerun verification")


def proposed(state: StateStore, finding_id: str) -> None:
    config = state.load_config()
    finding = state.load_finding(finding_id, config)
    finding.transition(
        FindingStatus.FIX_PROPOSED,
        actor=Actor.FIXER,
        action="fix_proposed",
        detail="Fixture patch proposed.",
        at=NOW,
    )
    finding.attempts.append(
        FixAttempt(
            number=1,
            fixer="command",
            branch=f"maida-heal/{finding.id}-a1",
            started_at=NOW,
            outcome="proposed",
            changed_paths=["prompts/agent.md"],
            diff_lines=1,
            pull_request_number=17,
            pull_request_url="https://github.com/maida-ai/example/pull/17",
        )
    )
    state.save_finding(finding, config)


def test_closure_passes_only_when_specific_metric_holdout_and_full_gate_pass(
    tmp_path: Path,
) -> None:
    state, repo, finding_id = scaffold(tmp_path)
    proposed(state, finding_id)

    closure = verify_closure(
        repo,
        finding_id,
        now=NOW,
        runner=ReportRunner(report(), report()),
    )

    assert closure.verdict == "closed"
    assert all(item.passed for item in closure.conditions)
    stored = state.load_finding(finding_id, state.load_config())
    assert stored.status is FindingStatus.CLOSED


def test_restart_after_terminal_write_replays_verified_event_without_rerunning_gate(
    tmp_path: Path,
) -> None:
    state, repo, finding_id = scaffold(tmp_path)
    proposed(state, finding_id)
    first = verify_closure(
        repo,
        finding_id,
        now=NOW,
        runner=ReportRunner(report(), report()),
    )
    journal = repo / ".maida-heal" / "events.jsonl"
    journal.unlink()
    for path in (repo / ".maida-heal" / "events").rglob("*.json"):
        path.unlink()

    recovered = verify_closure(
        repo,
        finding_id,
        now=NOW,
        runner=ExplodingRunner(),
    )
    verify_closure(repo, finding_id, now=NOW, runner=ExplodingRunner())

    assert recovered == first
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [event["type"] for event in events] == ["fix.verified"]
    assert len({event["event_id"] for event in events}) == 1


def test_full_handoff_emits_verified_event_and_never_calls_merge(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, repo, finding_id = scaffold(tmp_path)
    proposed(state, finding_id)
    manifest = load_gate_manifest(repo)
    manifest.activation = ActivationConfig(
        acknowledged_by="Operator <operator@example.test>",
        date=date(2026, 8, 11),
        statement="autonomous-fix-loop-authorized",
    )
    manifest.mode = LoopMode.FULL
    manifest.stream_modes = {
        stream_id: LoopMode.FULL for stream_id in manifest.stream_modes
    }
    save_gate_manifest(repo, manifest)
    merge_calls: list[bool] = []
    monkeypatch.setattr(
        "maida.heal.release.maybe_auto_merge",
        lambda *_args, **_kwargs: merge_calls.append(True),
    )

    closure = verify_closure(
        repo,
        finding_id,
        now=NOW,
        runner=ReportRunner(report(), report()),
    )

    assert closure.verdict == "closed"
    assert merge_calls == []
    events = [
        json.loads(line)
        for line in (repo / ".maida-heal" / "events.jsonl").read_text().splitlines()
    ]
    verified = next(item for item in events if item["type"] == "fix.verified")
    assert verified["data"]["release_mode"] == "handoff"
    assert verified["data"]["release_ready"] is True
    assert verified["data"]["closure_report"]["verdict"] == "closed"


def test_full_profile_respects_verify_only_stream_override(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, repo, finding_id = scaffold(tmp_path)
    proposed(state, finding_id)
    finding = state.load_finding(finding_id, state.load_config())
    manifest = load_gate_manifest(repo)
    manifest.activation = ActivationConfig(
        acknowledged_by="Operator <operator@example.test>",
        date=date(2026, 8, 11),
        statement="autonomous-fix-loop-authorized",
    )
    manifest.mode = LoopMode.FULL
    manifest.stream_modes[finding.stream_id] = LoopMode.VERIFY
    manifest.auto_merge = AutoMergeConfig(enabled_at=NOW)
    save_gate_manifest(repo, manifest)
    merge_calls: list[bool] = []
    monkeypatch.setattr(
        "maida.heal.release.maybe_auto_merge",
        lambda *_args, **_kwargs: merge_calls.append(True),
    )

    verify_closure(
        repo,
        finding_id,
        now=NOW,
        runner=ReportRunner(report(), report()),
    )

    assert merge_calls == []
    events = [
        json.loads(line)
        for line in (repo / ".maida-heal" / "events.jsonl").read_text().splitlines()
    ]
    verified = next(item for item in events if item["type"] == "fix.verified")
    assert verified["data"]["release_mode"] == "verify_only"
    assert verified["data"]["release_ready"] is False


def test_scaffold_maps_webhook_secret_name_into_ci_environment(
    tmp_path: Path,
) -> None:
    state, repo, _finding_id = scaffold(tmp_path)
    config = state.load_config()
    config.events = EventConfig(
        sinks=[
            WebhookSinkConfig(
                url="https://hooks.example.test/maida-heal",
                secret_env="MAIDA_HEAL_WEBHOOK_SECRET",
            )
        ]
    )
    state.save_config(config)

    enable_gate(
        state,
        config,
        MaidaCLI(state),
        command=["fixture-gate", "{report}", "{suite}"],
        holdout_command=["fixture-holdout", "{report}", "{suite}"],
        now=NOW,
    )

    workflow = (repo / ".github" / "workflows" / "maida-heal.yml").read_text()
    assert (
        "MAIDA_HEAL_WEBHOOK_SECRET: ${{ secrets.MAIDA_HEAL_WEBHOOK_SECRET }}"
        in workflow
    )
    assert "fixture-webhook-secret" not in workflow


def test_lower_stream_mode_skips_ci_closure_without_mutating_finding(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    state, repo, finding_id = scaffold(tmp_path)
    proposed(state, finding_id)
    finding = state.load_finding(finding_id, state.load_config())
    manifest = load_gate_manifest(repo)
    manifest.stream_modes[finding.stream_id] = LoopMode.PROPOSE
    save_gate_manifest(repo, manifest)

    with pytest.raises(VerificationNotEnabled, match="intentionally skipped"):
        verify_closure(
            repo,
            finding_id,
            now=NOW,
            runner=ReportRunner(report(), report()),
        )

    monkeypatch.chdir(repo)
    result = CliRunner().invoke(app, ["verify", finding_id, "--if-enabled"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "skipped"
    stored = state.load_finding(finding_id, state.load_config())
    assert stored.status is FindingStatus.FIX_PROPOSED
    workflow = (repo / ".github" / "workflows" / "maida-heal.yml").read_text()
    assert 'verify "$finding" --if-enabled' in workflow


@pytest.mark.parametrize(
    ("candidate", "holdout", "failed_condition"),
    [
        (report(step_verdict=None), report(), "specific_metrics"),
        (report(), report(step_verdict="fail", verdict="fail"), "holdouts"),
        (report(verdict="fail", extra_fail=True), report(), "no_new_failures"),
    ],
)
def test_green_unrelated_holdout_regression_and_new_failure_reject(
    tmp_path: Path,
    candidate: dict[str, object],
    holdout: dict[str, object],
    failed_condition: str,
) -> None:
    state, repo, finding_id = scaffold(tmp_path)
    proposed(state, finding_id)

    closure = verify_closure(
        repo,
        finding_id,
        now=NOW,
        runner=ReportRunner(candidate, holdout),
    )

    assert closure.verdict == "rejected"
    condition = next(
        item for item in closure.conditions if item.name == failed_condition
    )
    assert condition.passed is False
    assert (
        state.load_finding(finding_id, state.load_config()).status
        is FindingStatus.FIX_REJECTED
    )
    assert (
        state.load_finding(finding_id, state.load_config()).cooldown_until is not None
    )


def test_report_major_mismatch_refuses_without_transition(tmp_path: Path) -> None:
    state, repo, finding_id = scaffold(tmp_path)
    proposed(state, finding_id)

    with pytest.raises(
        ReportCompatibilityError, match="unsupported Maida report major"
    ):
        verify_closure(
            repo,
            finding_id,
            now=NOW,
            runner=ReportRunner(report(version="3.0.0"), report()),
        )

    assert (
        state.load_finding(finding_id, state.load_config()).status
        is FindingStatus.FIX_PROPOSED
    )


def test_stale_reports_are_removed_before_scenario_execution(tmp_path: Path) -> None:
    state, repo, finding_id = scaffold(tmp_path)
    proposed(state, finding_id)
    stale = repo / ".maida" / "heal" / "reports"
    stale.mkdir(parents=True)
    write_json(stale / f"{finding_id}-candidate.json", report())

    with pytest.raises(GateError, match="did not write its report"):
        verify_closure(repo, finding_id, now=NOW, runner=NoReportRunner())
