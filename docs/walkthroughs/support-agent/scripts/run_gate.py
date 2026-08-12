#!/usr/bin/env python3
"""Run the sample repository scenarios and ask the public Maida CLI for a verdict."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent import run_support_agent  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("candidate", "holdout"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--holdout", type=Path)
    parser.add_argument("--finding")
    arguments = parser.parse_args()
    if arguments.suite == "holdout" and (
        arguments.holdout is None or not arguments.holdout.is_file()
    ):
        print("holdout suite requires the generated holdout manifest", file=sys.stderr)
        return 2
    executable = shutil.which("maida")
    if executable is None:
        print("maida executable not found", file=sys.stderr)
        return 10

    with tempfile.TemporaryDirectory(prefix="support-agent-gate-") as temporary:
        data_dir = Path(temporary) / "maida"
        previous = os.environ.get("MAIDA_DATA_DIR")
        os.environ["MAIDA_DATA_DIR"] = str(data_dir)
        try:
            prefix = "HOLDOUT" if arguments.suite == "holdout" else "CANDIDATE"
            for index in range(30):
                run_support_agent(
                    REPO / "prompts" / "support-agent.md",
                    order_id=f"ORDER-{prefix}-{index:02d}",
                )
        finally:
            if previous is None:
                os.environ.pop("MAIDA_DATA_DIR", None)
            else:
                os.environ["MAIDA_DATA_DIR"] = previous

        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                executable,
                "drift",
                "--window",
                str(data_dir / "runs"),
                "--baseline",
                str(arguments.baseline),
                "--policy",
                str(arguments.policy),
                "--agent",
                "support-agent",
                "--format",
                "json",
                "--json-out",
                str(arguments.report),
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )
    if completed.returncode not in {0, 1}:
        print(completed.stderr.strip() or "Maida gate failed", file=sys.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
