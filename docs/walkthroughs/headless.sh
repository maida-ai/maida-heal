#!/bin/sh
set -eu

control_root="${1:-}"
if [ -z "$control_root" ]; then
  control_root="$(mktemp -d)"
fi
mkdir -p "$control_root"
cd "$control_root"

export MAIDA_HEAL_LANGFUSE_FIXTURE=1
export MAIDA_HEAL_FIXTURE_NOW=2026-08-11T12:00:00Z

maida-heal up
maida-heal status --json
maida-heal watch --once

test -s .maida-heal/config.yaml
test -s .maida-heal/events.jsonl
grep -q '"type":"finding.opened"' .maida-heal/events.jsonl

printf '%s\n' "HEADLESS WALKTHROUGH PASS — config in, events out"
