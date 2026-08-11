import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from maida_heal.models import ClosureReport, Finding, HealConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("filename", "model"),
    [
        ("finding-1.0.0.schema.json", Finding),
        ("closure-report-1.0.0.schema.json", ClosureReport),
        ("config-1.0.0.schema.json", HealConfig),
    ],
)
def test_shipped_schema_matches_strict_pydantic_contract(
    filename: str, model: type[BaseModel]
) -> None:
    payload = json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(payload)
    generated = model.model_json_schema()
    projected = {
        key: value
        for key, value in payload.items()
        if key not in {"$schema", "$id", "x-schema-version"}
    }
    assert projected == generated
    assert payload["x-schema-version"] == "1.0.0"


def test_readme_and_walkthroughs_keep_the_progressive_public_story() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    ordered = [
        readme.index("maida-heal demo"),
        readme.index("maida-heal up"),
        readme.index("maida-heal enable fixes"),
        readme.index("maida-heal enable gate"),
        readme.index("maida-heal enable auto-merge"),
        readme.index("## The boundary"),
    ]
    assert ordered == sorted(ordered)
    assert "The verifier never uses an LLM" in readme
    assert "The fix writer is replaceable" in readme
    assert "No Maida cloud, accounts, or telemetry" in readme
    lowered = readme.lower()
    for disallowed in (
        "observability",
        "monitoring",
        "reliability layer",
        "guardrails",
    ):
        assert disallowed not in lowered

    walkthroughs = ROOT / "docs" / "walkthroughs"
    expected = {
        "tier-0-demo.md": "uv run maida-heal demo",
        "tier-1-shadow.md": "uv run maida-heal up --yes",
        "tier-2-fixes.md": "maida-heal enable fixes",
        "tier-3-gate.md": "maida-heal enable gate",
        "tier-4-release.md": "maida-heal enable auto-merge",
    }
    for filename, command in expected.items():
        text = (walkthroughs / filename).read_text(encoding="utf-8")
        assert command in text
        assert "Offline check:" in text
