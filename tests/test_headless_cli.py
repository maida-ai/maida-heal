import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator
from pytest import MonkeyPatch
from typer.testing import CliRunner

from maida_heal.cli import app
from maida_heal.models import (
    ActivationConfig,
    FixesConfig,
    GateConfig,
    LoopMode,
)
from maida_heal.state import StateStore, read_json, write_json

FIXTURE_ENV = {
    "MAIDA_HEAL_LANGFUSE_FIXTURE": "1",
    "MAIDA_HEAL_FIXTURE_NOW": "2026-08-11T12:00:00Z",
}
ROOT = Path(__file__).resolve().parents[1]


def test_up_plan_discovers_without_writing_and_up_never_prompts(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    planned = runner.invoke(app, ["up", "--plan"], env=FIXTURE_ENV)

    assert planned.exit_code == 0, planned.output
    plan = json.loads(planned.stdout)
    assert plan["plan"] is True
    assert plan["mode"] == "shadow"
    assert plan["streams"][0]["enabled"] is True
    assert not (tmp_path / ".maida-heal").exists()

    applied = runner.invoke(app, ["up"], env=FIXTURE_ENV, input="must-not-be-read\n")

    assert applied.exit_code == 0, applied.output
    summary = json.loads(applied.stdout)
    assert summary["mode"] == "shadow"
    assert summary["streams"] == 1
    config = (tmp_path / ".maida-heal" / "config.yaml").read_text()
    assert "schema_version: 2.0.0" in config
    assert "mode: shadow" in config
    assert "enabled: true" in config

    repeated = runner.invoke(app, ["up"], env=FIXTURE_ENV)
    assert repeated.exit_code == 2
    assert "bootstrap-only" in repeated.stderr
    assert (tmp_path / ".maida-heal" / "config.yaml").read_text() == config


def test_status_json_is_stable_machine_health_surface(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["up"], env=FIXTURE_ENV).exit_code == 0

    result = runner.invoke(app, ["status", "--json"], env=FIXTURE_ENV)

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == "1.0.0"
    assert payload["mode"] == "shadow"
    assert payload["health"] in {"healthy", "degraded"}
    assert payload["autonomy"] == {
        "detect": True,
        "propose": False,
        "verify": False,
        "release": "none",
    }
    assert payload["event_stream"]["format"] == "jsonl"
    assert payload["streams"][0]["effective_mode"] == "shadow"
    schema = json.loads((ROOT / "schemas" / "status-1.0.0.schema.json").read_text())
    Draft202012Validator(schema).validate(payload)


def test_watch_stderr_is_structured_json_without_environment_secrets(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    environment = {
        **FIXTURE_ENV,
        "LANGFUSE_SECRET_KEY": "SENTINEL_LANGFUSE_SECRET",
    }
    assert runner.invoke(app, ["up"], env=environment).exit_code == 0

    result = runner.invoke(app, ["watch", "--once"], env=environment)

    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stderr.splitlines() if line]
    assert records
    assert records[0]["event"] == "watch.cycle_started"
    assert records[-1]["event"] == "watch.cycle_completed"
    assert all(item["schema_version"] == "1.0.0" for item in records)
    assert "SENTINEL_LANGFUSE_SECRET" not in result.stderr


def test_legacy_enable_disable_commands_are_replaced_by_config_commands() -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"])

    assert help_result.exit_code == 0
    command_names = {
        line.split()[1]
        for line in help_result.stdout.splitlines()
        if line.lstrip().startswith("│") and len(line.split()) > 1
    }
    assert "enable" not in command_names
    assert "disable" not in command_names
    assert "config" in help_result.stdout

    config_help = runner.invoke(app, ["config", "--help"])
    assert "validate" in config_help.stdout
    assert "apply" in config_help.stdout


def test_mode_transitions_change_only_the_documented_dispatch_capability(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["up"], env=FIXTURE_ENV).exit_code == 0
    state = StateStore(tmp_path)
    calls: list[str] = []

    def fake_propose(
        _state: object,
        _config: object,
        finding_id: str,
        **_kwargs: object,
    ) -> object:
        calls.append(finding_id)
        return SimpleNamespace(pull_request=None)

    monkeypatch.setattr("maida_heal.cli.propose_fix", fake_propose)
    monkeypatch.setattr("maida_heal.cli.refresh_human_merges", lambda *_args: [])

    shadow = runner.invoke(app, ["watch", "--once"], env=FIXTURE_ENV)
    assert shadow.exit_code == 0
    assert calls == []

    config = state.load_config()
    config.fixes = FixesConfig(
        repo="maida-ai/example",
        repo_local_path=str(tmp_path),
        fixer="command",
        command=["fixture-fixer"],
    )
    config.mode = LoopMode.PROPOSE
    state.save_config(config)
    proposed = runner.invoke(app, ["watch", "--once"], env=FIXTURE_ENV)
    assert proposed.exit_code == 0
    per_cycle = len(calls)
    assert per_cycle > 0

    config.gate = GateConfig(
        command=["fixture-gate", "{report}"],
        holdout_command=["fixture-holdout", "{report}"],
    )
    config.mode = LoopMode.VERIFY
    state.save_config(config)
    verified = runner.invoke(app, ["watch", "--once"], env=FIXTURE_ENV)
    assert verified.exit_code == 0
    assert len(calls) == per_cycle * 2

    config.activation = ActivationConfig(
        acknowledged_by="Operator <operator@example.test>",
        date=date(2026, 8, 11),
        statement="autonomous-fix-loop-authorized",
    )
    config.mode = LoopMode.FULL
    state.save_config(config)
    full = runner.invoke(app, ["watch", "--once"], env=FIXTURE_ENV)
    assert full.exit_code == 0
    assert len(calls) == per_cycle * 3


def test_manual_kill_switch_reports_scope_and_can_escalate_recurrence_pause(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["up"], env=FIXTURE_ENV).exit_code == 0
    state = StateStore(tmp_path)
    scoped_lock = {
        "schema_version": "1.0.0",
        "paused_at": "2026-08-11T12:00:00Z",
        "actor": "system",
        "scope": "fix_dispatch",
    }
    write_json(state.lock_path, scoped_lock)

    resumed = runner.invoke(app, ["resume"], env=FIXTURE_ENV)

    assert resumed.exit_code == 0, resumed.output
    events = [
        json.loads(line)
        for line in (state.root / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["type"] == "loop.resumed"
    assert events[-1]["data"]["scope"] == "fix_dispatch"

    write_json(state.lock_path, scoped_lock)
    paused = runner.invoke(app, ["pause"], env=FIXTURE_ENV)

    assert paused.exit_code == 0, paused.output
    assert read_json(state.lock_path)["scope"] == "all"
    events = [
        json.loads(line)
        for line in (state.root / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["type"] == "loop.paused"
    assert events[-1]["data"]["scope"] == "all"
