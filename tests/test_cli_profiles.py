"""CLI coverage for config-driven profile application."""

import json
import subprocess
from datetime import date
from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from maida import heal
from maida.heal.cli import app
from maida.heal.models import (
    ActivationConfig,
    AutoMergeConfig,
    FixesConfig,
    GateConfig,
    LoopMode,
)
from maida.heal.state import StateStore, load_gate_manifest

FIXTURE_ENV = {
    "MAIDA_HEAL_LANGFUSE_FIXTURE": "1",
    "MAIDA_HEAL_FIXTURE_NOW": "2026-08-11T12:00:00Z",
}


def git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def config_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "CLI Profile Tests")
    git(path, "config", "user.email", "cli-profiles@example.test")
    git(path, "remote", "add", "origin", "git@github.com:maida-ai/example.git")
    (path / "agent.py").write_text("print('fixture')\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def test_config_apply_scaffolds_verify_then_syncs_full_release_profile(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    attached = runner.invoke(app, ["up"], env=FIXTURE_ENV)
    assert attached.exit_code == 0, attached.output
    repo = config_repo(tmp_path / "repo")
    monkeypatch.setattr("maida.heal.cli.check_gh_auth", lambda: None)
    state = StateStore(tmp_path)
    config = state.load_config()
    config.fixes = FixesConfig(
        repo="maida-ai/example",
        repo_local_path=str(repo),
        fixer="command",
        command=["true"],
    )
    config.mode = LoopMode.PROPOSE
    state.save_config(config)

    propose = runner.invoke(app, ["config", "apply"], env=FIXTURE_ENV)
    assert propose.exit_code == 0, propose.output
    assert json.loads(propose.stdout)["files"] == []

    config.gate = GateConfig(
        command=["fixture-gate", "--report", "{report}"],
        holdout_command=["fixture-holdout", "--report", "{report}"],
    )
    config.mode = LoopMode.VERIFY
    state.save_config(config)
    verify = runner.invoke(app, ["config", "apply"], env=FIXTURE_ENV)
    assert verify.exit_code == 0, verify.output
    verify_payload = json.loads(verify.stdout)
    assert verify_payload["holdout_runs"] == 4
    assert verify_payload["training_runs"] == 10
    assert load_gate_manifest(repo).mode is LoopMode.VERIFY

    config.activation = ActivationConfig(
        acknowledged_by="Operator <operator@example.test>",
        date=date(2026, 8, 11),
        statement="autonomous-fix-loop-authorized",
    )
    config.mode = LoopMode.FULL
    state.save_config(config)
    full = runner.invoke(app, ["config", "apply"], env=FIXTURE_ENV)
    assert full.exit_code == 0, full.output
    manifest = load_gate_manifest(repo)
    assert manifest.mode is LoopMode.FULL
    assert manifest.auto_merge is None
    workflow = (repo / ".github" / "workflows" / "maida-heal.yml").read_text()
    assert "contents: read" in workflow
    assert "GH_TOKEN: ${{ github.token }}" in workflow

    config.auto_merge = AutoMergeConfig(max_diff_lines=120, daily_budget=2)
    state.save_config(config)
    automatic = runner.invoke(app, ["config", "apply"], env=FIXTURE_ENV)
    assert automatic.exit_code == 0, automatic.output
    assert load_gate_manifest(repo).auto_merge is not None
    workflow = (repo / ".github" / "workflows" / "maida-heal.yml").read_text()
    assert "contents: write" in workflow
