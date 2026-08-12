#!/usr/bin/env python3
"""Deterministic command writer: restore the reviewed one-attempt retry budget."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    prompt = Path("prompts/support-agent.md")
    if not prompt.is_file():
        print(f"missing prompt: {prompt}", file=sys.stderr)
        return 2
    before = prompt.read_text(encoding="utf-8")
    old = "max_lookup_attempts: 3"
    if before.count(old) != 1:
        print("expected one regressed retry setting", file=sys.stderr)
        return 2
    prompt.write_text(before.replace(old, "max_lookup_attempts: 1"), encoding="utf-8")
    print("restored max_lookup_attempts from 3 to 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
