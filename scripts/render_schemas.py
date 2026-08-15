"""Render checked-in JSON Schemas from the strict Pydantic contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel

from maida.heal.models import (
    EVENT_DATA_MODELS,
    ClosureReport,
    EventEnvelope,
    Finding,
    HealConfig,
    StatusReport,
)

ROOT = Path(__file__).resolve().parents[1]


def model_schema(model: type[BaseModel]) -> dict[str, object]:
    payload = model.model_json_schema()
    if model is EventEnvelope:
        # Pydantic validates the envelope's sibling ``type`` and ``data`` fields
        # together at runtime. Encode the same relationship for external consumers;
        # a plain union on ``data`` alone would accept a valid payload for the wrong
        # event type.
        payload["allOf"] = [
            {
                "if": {
                    "properties": {"type": {"const": event_type.value}},
                    "required": ["type"],
                },
                "then": {
                    "properties": {
                        "data": {
                            "$ref": f"#/$defs/{data_model.__name__}",
                        }
                    }
                },
            }
            for event_type, data_model in EVENT_DATA_MODELS.items()
        ]
    return payload


def render(filename: str, model: type[BaseModel], version: str) -> None:
    match = re.fullmatch(r"(.+)-([0-9]+\.[0-9]+\.[0-9]+)\.schema\.json", filename)
    if match is None or match.group(2) != version:
        raise ValueError(f"schema filename/version mismatch: {filename} / {version}")
    artifact = match.group(1)
    payload = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://maida.ai/schemas/maida-heal/{artifact}/{version}",
        "x-schema-version": version,
        **model_schema(model),
    }
    (ROOT / "maida" / "heal" / "schemas" / filename).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    render("finding-1.0.0.schema.json", Finding, "1.0.0")
    render("closure-report-1.0.0.schema.json", ClosureReport, "1.0.0")
    render("config-2.0.0.schema.json", HealConfig, "2.0.0")
    render("event-1.0.0.schema.json", EventEnvelope, "1.0.0")
    render("status-1.0.0.schema.json", StatusReport, "1.0.0")


if __name__ == "__main__":
    main()
