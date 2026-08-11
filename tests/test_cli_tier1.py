import json
from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch
from typer.testing import CliRunner, Result

from maida_heal.cli import app
from maida_heal.models import FixesConfig
from maida_heal.state import StateStore

runner = CliRunner()
FIXTURE_ENV = {
    "MAIDA_HEAL_LANGFUSE_FIXTURE": "1",
    "MAIDA_HEAL_FIXTURE_NOW": "2026-08-11T12:00:00Z",
}


def attach(tmp_path: Path, monkeypatch: MonkeyPatch) -> Result:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["up", "--yes"], env=FIXTURE_ENV)
    assert result.exit_code == 0, result.output
    return result


def test_up_tells_complete_shadow_story_and_names_one_next_step(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    result = attach(tmp_path, monkeypatch)

    assert "FIRST REPORT" in result.stdout
    assert "19 traces" in result.stdout
    assert "Attach" not in result.stdout
    assert "`maida-heal enable fixes`" in result.stdout
    assert "Connect —" in result.stderr
    assert "Report —" in result.stderr
    assert (tmp_path / ".maida-heal" / "config.yaml").is_file()


def test_status_findings_pause_resume_watch_and_purge(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    attach(tmp_path, monkeypatch)

    status = runner.invoke(app, ["status"], env=FIXTURE_ENV)
    assert status.exit_code == 0
    assert "Tier: 1" in status.stdout
    assert "Autonomous: findings only" in status.stdout
    assert "Next step: maida-heal enable fixes" in status.stdout

    listed = runner.invoke(app, ["findings", "list"], env=FIXTURE_ENV)
    assert listed.exit_code == 0
    finding_id = listed.stdout.split("\t", 1)[0]
    shown = runner.invoke(app, ["findings", "show", finding_id], env=FIXTURE_ENV)
    payload = json.loads(shown.stdout)
    assert payload["schema_version"] == "1.0.0"
    assert "raw_payload" not in payload

    paused = runner.invoke(app, ["pause"], env=FIXTURE_ENV)
    assert paused.exit_code == 0
    repeated_up = runner.invoke(app, ["up", "--yes"], env=FIXTURE_ENV)
    assert repeated_up.exit_code == 2
    purge_while_paused = runner.invoke(app, ["purge"], env=FIXTURE_ENV)
    assert purge_while_paused.exit_code == 2
    watch = runner.invoke(app, ["watch", "--once"], env=FIXTURE_ENV)
    assert watch.exit_code == 2
    assert "is paused" in watch.stderr
    resumed = runner.invoke(app, ["resume"], env=FIXTURE_ENV)
    assert resumed.exit_code == 0
    watch = runner.invoke(app, ["watch", "--once"], env=FIXTURE_ENV)
    assert watch.exit_code == 0, watch.output

    purge = runner.invoke(app, ["purge"], env=FIXTURE_ENV)
    assert purge.exit_code == 0
    assert "Purged" in purge.stdout
    assert not (tmp_path / ".maida-heal" / "imported" / "maida").exists()


def test_fix_refuses_cleanly_while_only_shadow_mode_is_enabled(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    attach(tmp_path, monkeypatch)
    listed = runner.invoke(app, ["findings", "list"], env=FIXTURE_ENV)
    finding_id = listed.stdout.split("\t", 1)[0]

    result = runner.invoke(app, ["fix", finding_id, "--dry-run"], env=FIXTURE_ENV)

    assert result.exit_code == 2
    assert "Fixes are not enabled" in result.stderr


def test_watch_dispatches_new_findings_only_when_auto_propose_is_enabled(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    attach(tmp_path, monkeypatch)
    state = StateStore(tmp_path)
    config = state.load_config()
    config.fixes = FixesConfig(
        repo="maida-ai/example",
        repo_local_path=str(tmp_path / "repo"),
        fixer="command",
        command=["fixture-fixer"],
        auto_propose=True,
    )
    state.save_config(config)
    for path in state.local_findings_dir.glob("mh-*.json"):
        path.unlink()
    dispatched: list[str] = []

    def fake_propose(
        _state: object,
        _config: object,
        finding_id: str,
        **_kwargs: object,
    ) -> object:
        dispatched.append(finding_id)
        return SimpleNamespace(pull_request=None)

    monkeypatch.setattr("maida_heal.cli.propose_fix", fake_propose)
    result = runner.invoke(app, ["watch", "--once"], env=FIXTURE_ENV)

    assert result.exit_code == 0, result.output
    assert dispatched
