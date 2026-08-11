from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from maida_heal.models import (
    Actor,
    Finding,
    FindingSource,
    FindingStatus,
    HealConfig,
    HistoryEvent,
    LangfuseConfig,
    MetricFailure,
)
from maida_heal.state import StateError, StateStore

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def finding() -> Finding:
    return Finding(
        id="mh-20260811-0123456789",
        stream="support-agent",
        stream_id="support-agent-aabbccdd",
        source=FindingSource.SHADOW_WATCH,
        title="step_count drift",
        summary="Structural failure.",
        detected_at=NOW,
        updated_at=NOW,
        metric_failures=[
            MetricFailure(
                metric="step_count",
                kind="distributional",
                run_ids=["a" * 32],
                evidence_pointer=".maida-heal/reports/report.json",
                decision_rule="wilson_one_sided",
            )
        ],
        history=[
            HistoryEvent(
                timestamp=NOW,
                actor=Actor.SYSTEM,
                action="finding_opened",
                detail="Detected drift.",
            )
        ],
    )


def test_finding_lifecycle_accepts_path_and_rejects_illegal_transition() -> None:
    item = finding()
    item.transition(
        FindingStatus.FIX_PROPOSED,
        actor=Actor.FIXER,
        action="fix_proposed",
        detail="Patch created.",
        at=NOW,
    )
    item.transition(
        FindingStatus.VERIFYING,
        actor=Actor.VERIFIER,
        action="verification_started",
        detail="Gate started.",
        at=NOW,
    )
    item.transition(
        FindingStatus.CLOSED,
        actor=Actor.VERIFIER,
        action="finding_closed",
        detail="Every closure condition passed.",
        at=NOW,
    )

    with pytest.raises(ValueError, match="illegal finding transition"):
        item.transition(
            FindingStatus.FIX_PROPOSED,
            actor=Actor.FIXER,
            action="retry",
            detail="Cannot reopen a closed finding.",
        )


def test_finding_models_forbid_unknown_fields_and_naive_timestamps() -> None:
    payload = finding().model_dump(mode="json")
    payload["raw_payload"] = "must never have a schema slot"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Finding.model_validate(payload)

    payload.pop("raw_payload")
    payload["detected_at"] = "2026-08-11T00:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        Finding.model_validate(payload)


def test_progressive_config_enforces_tier_order() -> None:
    tier_one = HealConfig(
        langfuse=LangfuseConfig(
            host="https://example.langfuse.test",
            credential_source="environment",
        )
    )
    assert tier_one.tier == 1

    with pytest.raises(ValidationError, match="gate requires fixes"):
        HealConfig.model_validate(
            {
                "schema_version": "1.0.0",
                "gate": {
                    "command": ["./verify"],
                    "holdout_fraction": 0.25,
                },
            }
        )


def test_state_root_refuses_a_symbolic_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".maida-heal").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StateError, match="must not be a symbolic link"):
        StateStore(tmp_path).initialize()
