from datetime import datetime, timedelta, timezone

from maida.heal.discovery import discover_streams
from maida.heal.langfuse import Observation
from maida.heal.onboarding import apply_stream_edits, candidate_to_config

START = datetime(2026, 8, 1, tzinfo=timezone.utc)


def observation(
    trace: str,
    *,
    name: str,
    session: str | None = None,
    metadata: dict[str, str] | None = None,
    index: int = 0,
    usage: float = 1,
    cost: float = 0,
) -> Observation:
    started = START + timedelta(minutes=index)
    return Observation(
        id=f"{trace}-observation",
        trace_id=trace,
        trace_name=name,
        session_id=session,
        start_time=started,
        end_time=started + timedelta(seconds=1),
        observation_type="GENERATION",
        name="respond",
        metadata=metadata or {},
        usage_total=usage,
        total_cost=cost,
    )


def test_stream_grouping_prioritizes_session_then_metadata_then_trace_name() -> None:
    observations = [
        observation(
            "session-one",
            name="shared-name",
            session="gateway-1001",
            metadata={"agent_id": "ignored"},
            index=1,
        ),
        observation(
            "session-two",
            name="shared-name",
            session="gateway-1002",
            metadata={"agent_id": "ignored"},
            index=2,
        ),
        observation(
            "metadata-one",
            name="mixed-a",
            metadata={"agent_id": "billing"},
            index=3,
        ),
        observation(
            "metadata-two",
            name="mixed-b",
            metadata={"agent_id": "billing"},
            index=4,
        ),
        observation("name-one", name="fallback", index=5),
        observation("name-two", name="fallback", index=6),
    ]

    streams = discover_streams(observations, metadata_keys=["agent_id"])

    assert [(item.grouping, item.trace_count) for item in streams] == [
        ("metadata", 2),
        ("session_pattern", 2),
        ("trace_name", 2),
    ]
    assert all(len(item.grouping_value_hash) == 12 for item in streams)
    assert "billing" not in next(
        item.name for item in streams if item.grouping == "metadata"
    )
    assert "gateway" not in next(
        item.name for item in streams if item.grouping == "session_pattern"
    )


def test_long_high_variance_stream_is_excluded_by_default() -> None:
    first = observation("one", name="interactive", index=1)
    second = observation("two", name="interactive", index=2)
    second = Observation(
        **{
            **second.__dict__,
            "end_time": second.start_time + timedelta(hours=2),
        }
    )

    [stream] = discover_streams([first, second], metadata_keys=[])

    assert stream.outlier is True
    assert stream.selected is False


def test_expensive_interactive_shape_is_excluded_by_default() -> None:
    observations = [
        observation("one", name="expensive", cost=2.0),
        observation("two", name="expensive", cost=3.0, index=1),
    ]

    [stream] = discover_streams(observations, metadata_keys=[])

    assert stream.outlier is True
    assert stream.selected is False


def test_merged_stream_persists_every_source_selector() -> None:
    observations = [
        observation("metadata-one", name="alpha", metadata={"agent_id": "a"}),
        observation("metadata-two", name="alpha", metadata={"agent_id": "a"}, index=1),
        observation("fallback-one", name="beta", index=2),
        observation("fallback-two", name="beta", index=3),
    ]
    candidates = discover_streams(observations, metadata_keys=["agent_id"])
    target = next(item for item in candidates if item.grouping == "metadata")
    source = next(item for item in candidates if item.grouping == "trace_name")

    [merged] = apply_stream_edits(
        candidates,
        select_all=True,
        merges={target.id: [source.id]},
    )
    configured = candidate_to_config(merged)

    assert len(configured.selectors) == 2
    assert {item.grouping for item in configured.selectors} == {
        "metadata",
        "trace_name",
    }
