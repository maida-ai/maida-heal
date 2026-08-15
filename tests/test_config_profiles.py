from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from maida.heal.models import (
    ActivationConfig,
    AutoMergeConfig,
    EnvelopeConfig,
    FixesConfig,
    GateConfig,
    HealConfig,
    LangfuseConfig,
    LoopMode,
    StreamConfig,
)

NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def fixes() -> FixesConfig:
    return FixesConfig(
        repo="maida-ai/example",
        repo_local_path="/tmp/example",
        fixer="command",
        command=["fixture-fixer"],
    )


def gate() -> GateConfig:
    return GateConfig(
        enabled_at=NOW,
        command=["fixture-gate", "--report", "{report}"],
        holdout_command=["fixture-holdout", "--report", "{report}"],
    )


def activation() -> ActivationConfig:
    return ActivationConfig(
        acknowledged_by="Operator <operator@example.test>",
        date=date(2026, 8, 11),
        statement="autonomous-fix-loop-authorized",
    )


def test_modes_require_exact_capability_blocks_and_full_attestation() -> None:
    with pytest.raises(ValidationError, match="propose mode requires fixes"):
        HealConfig(mode=LoopMode.PROPOSE)
    with pytest.raises(ValidationError, match="verify mode requires gate"):
        HealConfig(mode=LoopMode.VERIFY, fixes=fixes())
    with pytest.raises(
        ValidationError, match="full mode requires an activation attestation"
    ):
        HealConfig(mode=LoopMode.FULL, fixes=fixes(), gate=gate())

    configured = HealConfig(
        mode=LoopMode.FULL,
        fixes=fixes(),
        gate=gate(),
        activation=activation(),
    )
    assert configured.mode is LoopMode.FULL
    assert configured.release_mode == "handoff"

    configured.auto_merge = AutoMergeConfig(enabled_at=NOW)
    assert configured.release_mode == "auto_merge"


def test_auto_merge_is_rejected_outside_full_mode() -> None:
    with pytest.raises(ValidationError, match="auto_merge requires full mode"):
        HealConfig(
            mode=LoopMode.VERIFY,
            fixes=fixes(),
            gate=gate(),
            auto_merge=AutoMergeConfig(enabled_at=NOW),
        )


def test_stream_override_can_reduce_but_not_exceed_global_mode() -> None:
    stream = StreamConfig(
        id="support-agent",
        name="Support agent",
        grouping="trace_name",
        grouping_key="traceName",
        grouping_value_hash="aabbccddeeff",
        trace_names=["support-agent"],
        mode=LoopMode.SHADOW,
        envelope=EnvelopeConfig(
            coverage=0.99,
            metrics={"latency_ms": "report_only", "cost_tokens": "disabled"},
        ),
    )
    configured = HealConfig(
        mode=LoopMode.PROPOSE,
        langfuse=LangfuseConfig(
            host="https://langfuse.example.test",
            credential_source="environment",
        ),
        streams=[stream],
        fixes=fixes(),
    )
    assert configured.effective_mode(stream) is LoopMode.SHADOW
    assert stream.enabled is True
    assert stream.envelope.coverage == 0.99

    stream.mode = LoopMode.VERIFY
    with pytest.raises(ValidationError, match="cannot exceed global mode"):
        HealConfig.model_validate(configured.model_dump(mode="json"))


def test_activation_statement_is_literal_and_auditable() -> None:
    with pytest.raises(ValidationError):
        ActivationConfig(
            acknowledged_by="Operator",
            date=date(2026, 8, 11),
            statement="yes",
        )
