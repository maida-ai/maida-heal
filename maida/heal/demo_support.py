"""Private deterministic commands used by the bundled offline demo.

The patch command stands in for a replaceable fix writer. The gate command shells
out to Maida through the same public CLI adapter used everywhere else. Neither
command makes a network request, and the patch command never judges its output.
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from maida.heal.core import MaidaCLI
from maida.heal.fixtures import (
    fixed_candidate_runs,
    fixture_runs,
    materialize_runs,
)
from maida.heal.state import StateStore


def apply_patch_fixture(worktree: Path) -> int:
    prompt = worktree / "prompts" / "agent.md"
    if not prompt.is_file():
        return 2
    prompt.write_text("# Support agent\nretries: 1\n", encoding="utf-8")
    return 0


def run_gate(
    repo: Path,
    *,
    report: Path,
    baseline: Path,
    policy: Path,
    suite: str,
) -> int:
    healthy = "retries: 1" in (repo / "prompts" / "agent.md").read_text(
        encoding="utf-8"
    )
    # With a one-sided 90% distributional envelope, Maida needs enough trials to
    # conclude PASS even when there are zero harmful exceedances. Thirty keeps the
    # demo deterministic while satisfying the released verifier's confidence rule.
    healthy_runs = fixed_candidate_runs(30)
    if suite == "candidate":
        runs = healthy_runs if healthy else fixture_runs(regression=True)[-10:]
    elif suite == "holdout":
        runs = healthy_runs
    else:
        return 2
    with tempfile.TemporaryDirectory(prefix="maida-heal-demo-gate-") as temporary:
        root = Path(temporary)
        state = StateStore(root)
        materialize_runs(runs, state.data_dir)
        result = MaidaCLI(state).drift(
            window=state.runs_dir,
            baseline=baseline,
            policy=policy,
            agent="support-agent",
            report_path=report,
        )
    return 0 if result["verdict"] == "pass" else 1


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("patch")
    gate = subparsers.add_parser("gate")
    gate.add_argument("--report", type=Path, required=True)
    gate.add_argument("--baseline", type=Path, required=True)
    gate.add_argument("--policy", type=Path, required=True)
    gate.add_argument("--suite", choices=("candidate", "holdout"), required=True)
    gate.add_argument("--holdout", type=Path)
    gate.add_argument("--finding")
    arguments = parser.parse_args()
    if arguments.command == "patch":
        raise SystemExit(apply_patch_fixture(Path.cwd()))
    raise SystemExit(
        run_gate(
            Path.cwd(),
            report=arguments.report,
            baseline=arguments.baseline,
            policy=arguments.policy,
            suite=arguments.suite,
        )
    )


if __name__ == "__main__":
    main()
