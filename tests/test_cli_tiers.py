import subprocess
from datetime import datetime, timezone
from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from maida_heal.cli import app
from maida_heal.models import FindingStatus, MergeRecord
from maida_heal.state import StateStore, load_gate_manifest

FIXTURE_ENV = {
    "MAIDA_HEAL_LANGFUSE_FIXTURE": "1",
    "MAIDA_HEAL_FIXTURE_NOW": "2026-08-11T12:00:00Z",
}
NOW = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)


def git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)


def config_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "CLI Tier Tests")
    git(path, "config", "user.email", "cli-tiers@example.test")
    git(path, "remote", "add", "origin", "git@github.com:maida-ai/example.git")
    (path / "agent.py").write_text("print('fixture')\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def test_cli_progresses_through_and_walks_back_every_later_tier(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    runner = CliRunner()
    monkeypatch.chdir(tmp_path)
    attached = runner.invoke(app, ["up", "--yes"], env=FIXTURE_ENV)
    assert attached.exit_code == 0, attached.output
    repo = config_repo(tmp_path / "repo")
    monkeypatch.setattr("maida_heal.cli.check_gh_auth", lambda: None)

    fixes = runner.invoke(
        app,
        [
            "enable",
            "fixes",
            "--repo",
            str(repo),
            "--fixer",
            "command",
            "--command",
            "true",
            "--yes",
        ],
        env=FIXTURE_ENV,
    )
    assert fixes.exit_code == 0, fixes.output
    assert "Fixes will arrive as pull requests" in fixes.stdout

    gate = runner.invoke(
        app,
        [
            "enable",
            "gate",
            "--command",
            "fixture-gate --report {report}",
            "--holdout-command",
            "fixture-holdout --report {report}",
        ],
        env=FIXTURE_ENV,
    )
    assert gate.exit_code == 0, gate.output
    assert "Holdout split: 4 withheld; 10 training" in gate.stdout

    state = StateStore(tmp_path)
    config = state.load_config()
    watched = state.list_findings(config)[0]
    watched.status = FindingStatus.CLOSED
    watched.merge = MergeRecord(
        mode="human",
        merged_at=NOW,
        commit="abcdef1234567890",
        pull_request_number=17,
    )
    state.save_finding(watched, config)

    automatic = runner.invoke(
        app,
        [
            "enable",
            "auto-merge",
            "--yes",
            "--max-diff-lines",
            "120",
            "--daily-budget",
            "2",
        ],
        env=FIXTURE_ENV,
    )
    assert automatic.exit_code == 0, automatic.output
    assert automatic.stdout.startswith("AUTONOMOUS BEHAVIOR TO AUTHORIZE")
    assert load_gate_manifest(repo).auto_merge is not None

    status = runner.invoke(app, ["status"], env=FIXTURE_ENV)
    assert "Tier: 4" in status.stdout
    assert "120 diff lines; 2 merges/day" in status.stdout
    assert "revert pull requests always require human merge" in status.stdout

    disable_auto = runner.invoke(app, ["disable", "auto-merge"], env=FIXTURE_ENV)
    assert disable_auto.exit_code == 0
    assert "current tier: 3" in disable_auto.stdout
    disable_gate = runner.invoke(app, ["disable", "gate"], env=FIXTURE_ENV)
    assert disable_gate.exit_code == 0
    assert "current tier: 2" in disable_gate.stdout
    disable_fixes = runner.invoke(app, ["disable", "fixes"], env=FIXTURE_ENV)
    assert disable_fixes.exit_code == 0
    assert "current tier: 1" in disable_fixes.stdout
