# Tier 4: bounded release

After at least one verified finding PR has been merged by a person, inspect and
authorize the displayed envelope:

```bash
maida-heal enable auto-merge
maida-heal status
```

The command refuses without that watched merge. A closed PR over the diff limit, at
the daily budget, or under the kill switch stays for human review. A matching
recurrence within 48 hours opens a revert PR and pauses the loop; the revert is never
automatic.

Kill switch: `maida-heal pause`. Walk back only this tier with
`maida-heal disable auto-merge`.

Offline check: `uv run pytest -q tests/test_release.py`.
