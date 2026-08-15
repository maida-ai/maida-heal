import json
import os
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from maida.heal.models import (
    ClosureReport,
    EventEnvelope,
    Finding,
    HealConfig,
    StatusReport,
)
from scripts.render_schemas import model_schema

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("filename", "model", "version"),
    [
        ("finding-1.0.0.schema.json", Finding, "1.0.0"),
        ("closure-report-1.0.0.schema.json", ClosureReport, "1.0.0"),
        ("config-2.0.0.schema.json", HealConfig, "2.0.0"),
        ("event-1.0.0.schema.json", EventEnvelope, "1.0.0"),
        ("status-1.0.0.schema.json", StatusReport, "1.0.0"),
    ],
)
def test_shipped_schema_matches_strict_pydantic_contract(
    filename: str, model: type[BaseModel], version: str
) -> None:
    payload = json.loads((ROOT / "maida" / "heal" / "schemas" / filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(payload)
    generated = model_schema(model)
    projected = {
        key: value
        for key, value in payload.items()
        if key not in {"$schema", "$id", "x-schema-version"}
    }
    assert projected == generated
    assert payload["x-schema-version"] == version


def test_public_copy_leads_with_automated_handoff_and_config_profiles() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    ordered = [
        readme.index("Customer AI loop"),
        readme.index("maida-heal demo"),
        readme.index("maida-heal up --plan"),
        readme.index("## Configuration profiles"),
        readme.index("## Events are the API"),
        readme.index("## Handoff first"),
        readme.index("## Load-bearing boundaries"),
    ]
    assert ordered == sorted(ordered)
    assert "The verifier never uses an LLM" in readme
    assert "The fix writer is replaceable" in readme
    assert "Handoff mode never merges" in readme
    assert "No Maida cloud, account, license check" in readme
    assert "maida-heal enable" not in readme
    assert "maida-heal disable" not in readme

    public_files = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
    public_copy = "\n".join(path.read_text().lower() for path in public_files)
    for disallowed in (
        "observability",
        "monitoring",
        "reliability layer",
        "guardrails",
    ):
        assert disallowed not in public_copy


def test_walkthroughs_are_profile_driven_and_name_machine_events() -> None:
    walkthroughs = ROOT / "docs" / "walkthroughs"
    expected = {
        "demo.md": ("uv run maida-heal demo", "fix.verified"),
        "shadow.md": ("mode: shadow", "finding.opened"),
        "propose.md": ("mode: propose", "fix.proposed"),
        "verify.md": ("mode: verify", "fix.verified"),
        "full.md": ("mode: full", "release_ready: true"),
    }
    for filename, markers in expected.items():
        text = (walkthroughs / filename).read_text(encoding="utf-8")
        assert all(marker in text for marker in markers)
        assert "Offline check:" in text or "Executable check:" in text


def test_headless_walkthrough_runs_with_closed_stdin(
    tmp_path: Path,
) -> None:
    script = ROOT / "docs" / "walkthroughs" / "headless.sh"
    completed = subprocess.run(
        [str(script), str(tmp_path)],
        cwd=ROOT,
        env=dict(os.environ),
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "HEADLESS WALKTHROUGH PASS" in completed.stdout
    events = [
        json.loads(line)
        for line in (tmp_path / ".maida-heal" / "events.jsonl").read_text().splitlines()
    ]
    ids = [item["event_id"] for item in events]
    assert ids
    assert len(ids) == len(set(ids))


def test_reference_deployment_artifacts_match_the_watch_shape() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    deploy = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")

    assert "uv sync --frozen --no-dev" in dockerfile
    assert 'PATH="/opt/maida-heal/.venv/bin:${PATH}"' in dockerfile
    assert 'CMD ["watch", "--interval", "300"]' in dockerfile
    assert "pip install" not in dockerfile
    assert "[Service]" in deploy
    assert "status --json" in deploy
    assert "maida-heal pause" in deploy
