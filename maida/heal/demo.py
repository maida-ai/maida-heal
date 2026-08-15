"""The complete offline detect-fix-verify-close terminal story."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from maida.heal.core import MaidaCLI
from maida.heal.events import EventJournal
from maida.heal.gate import closure_markdown, enable_gate, verify_closure
from maida.heal.gitops import Worktree
from maida.heal.healing import Publisher, PullRequest, propose_fix
from maida.heal.models import (
    ActivationConfig,
    ClosureReport,
    FixesConfig,
    JsonlSinkConfig,
    LoopMode,
)
from maida.heal.onboarding import (
    apply_stream_edits,
    attach,
    fixture_attachment_client,
)
from maida.heal.state import StateStore

DEMO_NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


class LocalDemoPublisher(Publisher):
    """Stand in for PR delivery without a remote, credential, or network call."""

    def publish(
        self,
        worktree: Worktree,
        finding: object,
        *,
        repository: str,
        body: str,
    ) -> PullRequest:
        del worktree, finding, repository
        if "production trace payloads" not in body:
            raise ValueError("demo PR body lost its payload-minimization statement")
        return PullRequest(1, "local://demo/pull/1")


@dataclass(frozen=True)
class DemoResult:
    finding_id: str
    detection_report: str
    changed_paths: tuple[str, ...]
    diff_lines: int
    closure: ClosureReport
    events: tuple[dict[str, object], ...]
    pr_comment: str


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def _initialize_demo_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Maida Heal Demo")
    _git(path, "config", "user.email", "demo@maida.invalid")
    prompts = path / "prompts"
    prompts.mkdir()
    (prompts / "agent.md").write_text("# Support agent\nretries: 3\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "fixture agent configuration")
    return path


def _gate_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "maida.heal.demo_support",
        "gate",
        "--report",
        "{report}",
        "--baseline",
        "{baseline}",
        "--policy",
        "{policy}",
        "--holdout",
        "{holdout}",
        "--finding",
        "{finding}",
        "--suite",
        "{suite}",
    ]


def run_demo() -> DemoResult:
    """Run the actual local loop and return buffered, payload-free evidence."""
    with tempfile.TemporaryDirectory(prefix="maida-heal-demo-") as temporary:
        root = Path(temporary)
        control = root / "control"
        control.mkdir()
        state = StateStore(control)
        batch, client = fixture_attachment_client()
        attached = attach(
            state,
            MaidaCLI(state),
            client,
            host="fixture://langfuse",
            credential_source="fixture",
            now=DEMO_NOW,
            metadata_keys=["agent_id"],
            configure=lambda candidates: apply_stream_edits(
                candidates, select_all=True
            ),
            progress=lambda _line: None,
            fixture_batch=batch,
        )
        finding = next(
            item for item in state.list_findings() if "step_count" in item.metric_names
        )
        report = next(
            detection.reports[0]
            for detection in attached.detections
            if finding.id in detection.findings_created
        )

        repo = _initialize_demo_repo(root / "agent-config")
        config = state.load_config()
        config.events.sinks = [JsonlSinkConfig(path=str(state.root / "events.jsonl"))]
        config.fixes = FixesConfig(
            repo="local/demo",
            repo_local_path=str(repo),
            fixer="command",
            command=[sys.executable, "-m", "maida.heal.demo_support", "patch"],
            cooldown_hours=0,
        )
        state.save_config(config)
        command = _gate_command()
        enable_gate(
            state,
            config,
            MaidaCLI(state),
            command=command,
            holdout_command=command,
            now=DEMO_NOW,
        )
        config = state.load_config()
        config.activation = ActivationConfig(
            acknowledged_by="Maida-heal offline demo",
            date=date(2026, 8, 11),
            statement="autonomous-fix-loop-authorized",
        )
        config.mode = LoopMode.FULL
        state.save_config(config)
        enable_gate(
            state,
            config,
            MaidaCLI(state),
            command=command,
            holdout_command=command,
            now=DEMO_NOW,
        )
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "fixture deterministic gate")

        proposed = propose_fix(
            state,
            state.load_config(),
            finding.id,
            now=DEMO_NOW,
            dry_run=False,
            publisher=LocalDemoPublisher(),
        )
        current = state.load_config()
        state.reconcile_finding_events(current)
        EventJournal(state.project_root, current.events).flush()
        _git(repo, "switch", proposed.branch)
        closure = verify_closure(repo, finding.id, now=DEMO_NOW)
        event_path = state.root / "events.jsonl"
        events = tuple(
            payload
            for line in event_path.read_text(encoding="utf-8").splitlines()
            if (payload := json.loads(line)).get("finding_id") == finding.id
        )
        return DemoResult(
            finding_id=finding.id,
            detection_report=report.relative_to(control).as_posix(),
            changed_paths=proposed.inspection.changed_paths,
            diff_lines=proposed.inspection.diff_lines,
            closure=closure,
            events=events,
            pr_comment=closure_markdown(closure),
        )
