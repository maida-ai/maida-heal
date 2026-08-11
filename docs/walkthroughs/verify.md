# Verify: deterministic closure

The operator supplies repository scenario commands and changes `mode`:

```yaml
mode: verify
gate:
  command: [./scripts/heal-candidate, --report, "{report}"]
  holdout_command: [./scripts/heal-holdout, --report, "{report}"]
  holdout_fraction: 0.25
```

`maida-heal config apply` promotes the reviewed generated policy, creates the
deterministic holdout split, and writes the heal-branch workflow. On a proposed PR,
CI runs the unchanged candidate gate and holdouts.

The system closes only if the finding's exact metrics pass, all holdouts pass, and no
new failure exists. It emits `fix.verified` with `release_mode: verify_only` and
`release_ready: false`, or `fix.rejected`. It never weakens policy or merges.
Streams whose config override remains `shadow` or `propose` are skipped cleanly by
the generated workflow and never gain closure semantics.

Offline check:

```bash
uv run pytest -q tests/test_gate.py
```
