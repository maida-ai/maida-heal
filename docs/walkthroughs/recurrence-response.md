# Recurrence response: deployment regresses again

Closure proves the candidate against repository scenarios. Production watch supplies
the later evidence that the deployed behavior actually recovered. This walkthrough
shows the adverse case: customer automation releases a handoff PR, then the same
support-agent metric fails again within the configured 48-hour recurrence window.

## Starting state

```text
prior finding       mh-20260811-0123456789
stream              support-agent-4674e386
recovered metrics   step_count, tool_call_count
release mode        handoff
customer deployment deploy-2026-08-11-42
watch state         running with an absolute persisted cursor
```

Handoff means the customer performed the merge and deployment. The recurrence join
uses Maida-heal's persisted finding/release history and the same stream-plus-metric
identity; it does not inspect customer payload content.

## Automated sequence

1. The next watch cycle imports an overlapping absolute time range and deduplicates
   previously seen run IDs.
2. Maida reports `step_count` failing again for
   `support-agent-4674e386`.
3. Detection opens a new `post_merge_watch` finding and records structural evidence.
4. The recurrence check matches the prior released finding inside 48 hours.
5. Maida-heal creates a revert branch and opens a revert PR. The revert is never
   merged automatically.
6. It emits `rollback.opened` with both finding IDs, branch, and PR URL.
7. It writes the kill switch with scope `fix_dispatch`, preventing another writer
   attempt while detection continues.

The event delivered to customer automation has this shape:

```json
{
  "schema_version": "1.0.0",
  "event_id": "evt_<stable-id>",
  "type": "rollback.opened",
  "stream_id": "support-agent-4674e386",
  "finding_id": "mh-20260812-abcdef0123",
  "data": {
    "finding_id": "mh-20260812-abcdef0123",
    "prior_finding_id": "mh-20260811-0123456789",
    "pull_request_url": "https://github.example/customer/config/pull/43",
    "branch": "maida-heal/revert-mh-20260811-0123456789"
  }
}
```

No trace payload appears in the finding, event, revert body, or logs.

## Customer response

Customer automation should treat `rollback.opened` as an incident signal, not as a
release authorization:

1. Stop claiming pending release requests for this stream.
2. Page or notify the owning engineering team through the customer's existing path.
3. Inspect the revert PR and the newly referenced structural evidence.
4. Decide whether to merge the revert, ship another correction, or accept the
   changed behavior by changing policy in a separate reviewed change.

Operator commands remain non-interactive:

```bash
maida-heal status --json
maida-heal findings show mh-20260812-abcdef0123
maida-heal pause
```

`pause` broadens the kill switch to all mutating activity if the operator wants a
full stop. Import/comparison can still be run for diagnosis where the lock contract
allows it; no fix dispatch proceeds.

After the team resolves the incident:

```bash
maida-heal resume
maida-heal status --json
maida-heal watch --once
```

`resume` clears the explicit lock and emits `loop.resumed`. It does not merge the
revert PR, close the new finding, or erase attempt history. The next cycle resumes
from persisted cursors and deduplicated event IDs.

## Why the same response applies to automatic merge

With the separately attested `auto_merge` section, Maida-heal may have performed the
original merge inside its diff and budget bounds. The recurrence response is still a
revert PR plus paused fix dispatch; the revert never auto-merges. Handoff changes who
performed the original release, not the need for a human decision during rollback.

Executable check:

```bash
uv run pytest -q tests/test_release.py tests/test_watch_resilience.py
```
