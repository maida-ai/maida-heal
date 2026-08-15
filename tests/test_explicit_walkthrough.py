from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from maida.heal.core import MaidaCLI
from maida.heal.gate import enable_gate
from maida.heal.models import EventEnvelope, FixesConfig, HealConfig, LoopMode
from maida.heal.onboarding import apply_stream_edits, attach, fixture_attachment_client
from maida.heal.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs" / "walkthroughs" / "support-agent"


def _run_agent(prompt: Path, data_dir: Path) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(EXAMPLE / "agent.py"),
            "--prompt",
            str(prompt),
            "--json",
        ],
        cwd=EXAMPLE,
        env={**os.environ, "MAIDA_DATA_DIR": str(data_dir)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    return payload


def test_sample_agent_makes_the_retry_regression_visible(tmp_path: Path) -> None:
    regressed = _run_agent(
        EXAMPLE / "prompts" / "support-agent.md", tmp_path / "regressed"
    )
    fixed = _run_agent(
        EXAMPLE / "prompts" / "support-agent.fixed.md", tmp_path / "fixed"
    )

    assert regressed["configured_max_lookup_attempts"] == 3
    assert regressed["tool_path"] == [
        "lookup_order",
        "lookup_order",
        "lookup_order",
        "escalate_to_human",
    ]
    assert regressed["tool_call_count"] == 4
    assert fixed["configured_max_lookup_attempts"] == 1
    assert fixed["tool_path"] == ["lookup_order", "escalate_to_human"]
    assert fixed["tool_call_count"] == 2
    assert len(list((tmp_path / "regressed" / "runs").iterdir())) == 1
    assert len(list((tmp_path / "fixed" / "runs").iterdir())) == 1


def test_command_writer_changes_only_the_prompt_retry_budget(tmp_path: Path) -> None:
    repo = tmp_path / "support-agent"
    shutil.copytree(EXAMPLE, repo)
    before = {
        path.relative_to(repo): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file()
    }

    completed = subprocess.run(
        [sys.executable, "scripts/fix_retry_budget.py"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    after = {
        path.relative_to(repo): path.read_bytes()
        for path in repo.rglob("*")
        if path.is_file()
    }
    changed = [path for path in before if before[path] != after[path]]
    assert changed == [Path("prompts/support-agent.md")]
    prompt = (repo / changed[0]).read_text(encoding="utf-8")
    assert "max_lookup_attempts: 1" in prompt
    assert "max_lookup_attempts: 3" not in prompt


def test_sample_scenario_command_gets_its_verdict_from_maida(tmp_path: Path) -> None:
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
        now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
        metadata_keys=["agent_id"],
        configure=lambda candidates: apply_stream_edits(candidates, select_all=True),
        progress=lambda _line: None,
        fixture_batch=batch,
    )

    repo = tmp_path / "support-agent"
    shutil.copytree(EXAMPLE, repo)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    config = state.load_config()
    config.fixes = FixesConfig(
        repo="local/support-agent",
        repo_local_path=str(repo),
        fixer="command",
        command=[sys.executable, "scripts/fix_retry_budget.py"],
        allowed_paths=["prompts/**"],
    )
    state.save_config(config)
    gate_command = [
        sys.executable,
        "scripts/run_gate.py",
        "--suite",
        "{suite}",
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
    ]
    enable_gate(
        state,
        config,
        MaidaCLI(state),
        command=gate_command,
        holdout_command=gate_command,
        now=datetime(2026, 8, 11, 12, tzinfo=timezone.utc),
    )
    targets = list((repo / ".maida" / "heal" / "streams").glob("*/*/target.json"))
    assert len(targets) == 1
    target = targets[0].parent
    stream_id = targets[0].parents[1].name
    holdout = repo / ".maida" / "holdout" / stream_id / target.name / "manifest.json"

    def run_scenario(suite: str, report: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "scripts/run_gate.py",
                "--suite",
                suite,
                "--report",
                str(report),
                "--baseline",
                str(target / "baseline.json"),
                "--policy",
                str(target / "policy.yaml"),
                "--holdout",
                str(holdout),
            ],
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )

    regressed_report = tmp_path / "regressed-report.json"
    regressed = run_scenario("candidate", regressed_report)
    assert regressed.returncode == 1, regressed.stderr
    assert json.loads(regressed_report.read_text())["verdict"] == "fail"

    fixed = subprocess.run(
        [sys.executable, "scripts/fix_retry_budget.py"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    assert fixed.returncode == 0, fixed.stderr
    candidate_report = tmp_path / "candidate-report.json"
    candidate = run_scenario("candidate", candidate_report)
    assert candidate.returncode == 0, candidate.stderr
    assert json.loads(candidate_report.read_text())["verdict"] == "pass"
    holdout_report = tmp_path / "holdout-report.json"
    withheld = run_scenario("holdout", holdout_report)
    assert withheld.returncode == 0, withheld.stderr
    assert json.loads(holdout_report.read_text())["verdict"] == "pass"


def test_walkthrough_profiles_are_complete_valid_and_monotonic() -> None:
    expected = [
        ("01-shadow.yaml", LoopMode.SHADOW),
        ("02-propose.yaml", LoopMode.PROPOSE),
        ("03-verify.yaml", LoopMode.VERIFY),
        ("04-full-handoff.yaml", LoopMode.FULL),
    ]
    configs: list[HealConfig] = []
    for filename, mode in expected:
        payload = yaml.safe_load((EXAMPLE / "config" / filename).read_text())
        config = HealConfig.model_validate(payload)
        assert config.mode is mode
        configs.append(config)

    shadow, propose, verify, full = configs
    assert shadow.fixes is None and shadow.gate is None
    assert propose.fixes is not None and propose.gate is None
    assert verify.fixes is not None and verify.gate is not None
    assert full.fixes is not None and full.gate is not None
    assert full.activation is not None
    assert full.auto_merge is None
    assert full.release_mode == "handoff"
    serialized = json.dumps(
        [item.model_dump(mode="json") for item in configs], sort_keys=True
    )
    assert "replace-with-a-secret" not in serialized
    assert "MAIDA_HEAL_WEBHOOK_SECRET" in serialized


def test_customer_handoff_consumer_verifies_hmac_and_deduplicates(
    tmp_path: Path,
) -> None:
    event_path = EXAMPLE / "events" / "fix.verified.json"
    body = event_path.read_bytes()
    EventEnvelope.model_validate_json(body)
    secret = "test-only-shared-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    database = tmp_path / "handoff.sqlite3"
    command = [
        sys.executable,
        str(EXAMPLE / "automation" / "consume_event.py"),
        "--body",
        str(event_path),
        "--database",
        str(database),
    ]
    environment = {**os.environ, "MAIDA_HEAL_WEBHOOK_SECRET": secret}

    rejected = subprocess.run(
        [*command, "--signature", "sha256=wrong"],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert "signature mismatch" in rejected.stderr
    assert not database.exists()

    accepted = subprocess.run(
        [*command, "--signature", signature],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "queued release request" in accepted.stdout

    duplicate = subprocess.run(
        [*command, "--signature", signature],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert duplicate.returncode == 0, duplicate.stderr
    assert "duplicate event" in duplicate.stdout

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone() == (1,)
        assert connection.execute(
            "SELECT finding_id, pull_request_number, status FROM release_requests"
        ).fetchall() == [("mh-20260811-0123456789", 42, "pending")]


def test_explicit_walkthroughs_are_linked_and_show_the_customer_boundary() -> None:
    index = (ROOT / "docs" / "walkthroughs" / "README.md").read_text()
    readme = (ROOT / "README.md").read_text()
    for filename in (
        "support-agent-loop.md",
        "customer-handoff.md",
        "recurrence-response.md",
    ):
        assert filename in index
    assert "support-agent-loop.md" in readme

    loop = (ROOT / "docs" / "walkthroughs" / "support-agent-loop.md").read_text()
    assert "max_lookup_attempts: 3" in loop
    assert "lookup_order -> lookup_order -> lookup_order" in loop
    assert "maida-heal up --plan" in loop
    assert "maida-heal watch --once" in loop
    assert "fix.verified" in loop
    assert "customer-owned" in loop
