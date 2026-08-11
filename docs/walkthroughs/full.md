# Full: release handoff

The operator records authorization and changes `mode`:

```yaml
mode: full
activation:
  acknowledged_by: "Operator Name <operator@example.com>"
  date: 2026-08-11
  statement: autonomous-fix-loop-authorized
```

Without this exact block, config validation refuses. After `config apply`, successful
closure emits `fix.verified` with `release_mode: handoff`, `release_ready: true`, and
the complete closure report. Customer automation validates/deduplicates that event,
then owns merge and deployment. Maida-heal stops at handoff.

The optional stricter-envelope variant adds:

```yaml
auto_merge:
  max_diff_lines: 200
  daily_budget: 3
  recurrence_hours: 48
```

This changes release mode to `auto_merge`; it does not change closure. A matching
post-merge recurrence opens a revert PR, emits `rollback.opened`, and pauses fix
dispatch. Revert PRs always wait for customer action.

Offline check:

```bash
uv run pytest -q tests/test_release.py tests/test_config_profiles.py
```
