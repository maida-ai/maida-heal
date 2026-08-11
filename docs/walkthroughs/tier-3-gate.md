# Tier 3: deterministic closure

Provide the connected repository's candidate and withheld-scenario commands. Each
must write a Maida report to `{report}`:

```bash
maida-heal enable gate \
  --command './scripts/heal-candidate --report {report}' \
  --holdout-command './scripts/heal-holdout --report {report}'
```

Review the printed scaffold diff and commit it in the connected repository. Heal
branches then run `maida-heal verify FINDING_ID` in CI. Closure requires the exact
metric, all withheld scenarios, and the full gate to pass.

Offline check: `uv run pytest -q tests/test_gate.py`.
