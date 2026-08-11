# Tier 2: propose fixes

From the tier-1 state directory, connect the repository that controls agent behavior:

```bash
maida-heal enable fixes --repo /path/to/agent-config
maida-heal fix FINDING_ID --dry-run
maida-heal fix FINDING_ID
```

The dry run creates no branch or finding event. The real fix uses an isolated
worktree, enforces protected paths after the writer exits, commits an authorship
trailer, pushes the branch, and opens a PR. Before tier 3, that PR says verification
is not enabled and waits for manual review.

Offline check: `uv run pytest -q tests/test_healing.py tests/test_data_safety.py`.
