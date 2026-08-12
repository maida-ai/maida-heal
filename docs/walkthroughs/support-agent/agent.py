#!/usr/bin/env python3
"""Small deterministic agent loop used by the explicit walkthrough.

The local model and tools are fixtures, so this file makes no network calls. Maida
records the same structural events that a repository scenario command would expose
to the gate. Production detection remains a read-only import from Langfuse.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from maida import (  # type: ignore[import-untyped]
    record_llm_call,
    record_tool_call,
    traced_run,
)


@dataclass(frozen=True)
class AgentResult:
    configured_max_lookup_attempts: int
    tool_path: list[str]
    tool_call_count: int
    outcome: str


def retry_budget(prompt_path: Path) -> int:
    """Read the one behavior setting that this example intentionally changes."""
    matches = re.findall(
        r"^max_lookup_attempts:\s*([0-9]+)\s*$",
        prompt_path.read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise ValueError("prompt must define max_lookup_attempts exactly once")
    budget = int(matches[0])
    if not 1 <= budget <= 5:
        raise ValueError("max_lookup_attempts must be between 1 and 5")
    return budget


def lookup_order(order_id: str) -> dict[str, str]:
    """Return a deterministic ambiguous result that exercises the retry path."""
    return {"order_id": order_id, "status": "ambiguous"}


def run_support_agent(prompt_path: Path, *, order_id: str) -> AgentResult:
    """Run one visible decide -> tool -> retry/escalate agent loop."""
    budget = retry_budget(prompt_path)
    tools: list[str] = []
    with traced_run(name="support-agent"):
        record_llm_call(
            model="fixture-support-router",
            prompt={"task": "resolve_order", "order_id": order_id},
            response={"action": "lookup_order"},
            provider="fixture",
            usage={"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
        )
        for attempt in range(1, budget + 1):
            result = lookup_order(order_id)
            record_tool_call(
                name="lookup_order",
                args={"order_id": order_id},
                result=result,
                meta={"attempt": attempt},
            )
            tools.append("lookup_order")
            if result["status"] == "confirmed":
                return AgentResult(budget, tools, len(tools), "resolved")

        record_tool_call(
            name="escalate_to_human",
            args={"order_id": order_id, "reason": "ambiguous_order"},
            result={"queued": True},
        )
        tools.append("escalate_to_human")
    return AgentResult(budget, tools, len(tools), "escalated")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--order-id", default="ORDER-DEMO-42")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    result = run_support_agent(arguments.prompt, order_id=arguments.order_id)
    if arguments.json:
        print(json.dumps(asdict(result), sort_keys=True))
    else:
        print(f"Configured lookup attempts: {result.configured_max_lookup_attempts}")
        print(f"Tool path: {' -> '.join(result.tool_path)}")
        print(f"Outcome: {result.outcome}")
        print("A Maida trace was written beneath MAIDA_DATA_DIR/runs/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
