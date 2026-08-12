# Explicit support-agent loop: production drift to verified handoff

This walkthrough keeps every moving part visible. The sample is a support agent that
looks up an order, decides whether to retry, and escalates when the result remains
ambiguous. A prompt change raises the retry budget from one to three. The final
customer-facing outcome is still an escalation, but the execution path becomes
slower and more expensive:

```text
reviewed behavior: lookup_order -> escalate_to_human
changed behavior:  lookup_order -> lookup_order -> lookup_order -> escalate_to_human
```

That is the customer pain this loop addresses: an answer can still look acceptable
while structural behavior regresses.

The complete teaching repository is [`support-agent/`](support-agent/). It includes
the agent, both prompt states, the deterministic command writer, a real Maida gate
scenario command, four complete configs, one `fix.verified` event, and a
customer-owned event consumer.

## 1. Inspect the agent instead of trusting a canned result

From the `maida-heal` checkout:

```bash
sed -n '1,220p' docs/walkthroughs/support-agent/agent.py
sed -n '1,80p' docs/walkthroughs/support-agent/prompts/support-agent.md
```

The loop is deliberately ordinary:

1. A deterministic local router chooses `lookup_order`.
2. `lookup_order` returns `ambiguous`.
3. The prompt's `max_lookup_attempts` setting controls the retry loop.
4. Exhausting the budget calls `escalate_to_human`.
5. Public Maida SDK calls record the LLM decision and each tool call.

No provider or network is involved in this teaching agent. Run the changed prompt:

```bash
example_data=$(mktemp -d)
MAIDA_DATA_DIR="$example_data/regressed" \
  uv run python docs/walkthroughs/support-agent/agent.py \
  --prompt docs/walkthroughs/support-agent/prompts/support-agent.md
```

Expected structural output:

```text
Configured lookup attempts: 3
Tool path: lookup_order -> lookup_order -> lookup_order -> escalate_to_human
Outcome: escalated
A Maida trace was written beneath MAIDA_DATA_DIR/runs/.
```

Now run the reviewed prompt against the same input:

```bash
MAIDA_DATA_DIR="$example_data/fixed" \
  uv run python docs/walkthroughs/support-agent/agent.py \
  --prompt docs/walkthroughs/support-agent/prompts/support-agent.fixed.md
```

The outcome remains `escalated`, but the tool path is now:

```text
lookup_order -> escalate_to_human
```

The `.fixed.md` file exists only to make this teaching comparison inspectable. In a
customer repository, the writer produces that change on a heal branch.

## 2. Understand the two trace paths

The attached production agent already sends traces to Langfuse. Maida-heal reads
those traces to detect a change after deployment. The sample's local Maida tracing
has a different job: it represents the connected repository's deterministic CI
scenarios, which run candidate code before release.

```text
production agent -> Langfuse -> read-only import -> finding

candidate branch -> repository scenarios -> Maida gate -> closure report
```

CI does not replay customer production payloads. Post-release watch is the true
production confirmation. The walkthrough keeps this boundary explicit because a
local scenario PASS and a production recovery are different claims.

## 3. Attach production traces in `shadow`

In a durable control directory, expose the credentials the production Langfuse SDK
already uses:

```bash
export LANGFUSE_PUBLIC_KEY=pk-lf-existing
export LANGFUSE_SECRET_KEY=sk-lf-existing
export LANGFUSE_HOST=https://langfuse.example.com
maida-heal up --plan
```

`--plan` performs read-only connection and discovery, then prints every intended
write without creating `.maida-heal/`. Review the absolute 14-day window and the
inferred stream selectors. Apply the same plan:

```bash
maida-heal up
```

The command writes and immediately exercises:

```text
.maida-heal/config.yaml                 selected stream definitions
.maida-heal/streams/<stream>/policy.yaml generated conservative policy
.maida-heal/imports.json                absolute cursors and imported run IDs
.maida-heal/reports/                    first real comparison
.maida-heal/events.jsonl                machine event journal
```

Compare the generated file with the complete teaching profile in
[`01-shadow.yaml`](support-agent/config/01-shadow.yaml). Real grouping hashes are
derived from the customer's metadata and must not be replaced by the teaching
values. The important stream section is explicit and editable:

```yaml
streams:
  - id: support-agent-4674e386
    name: Support order agent
    grouping: metadata
    grouping_key: agent_id
    grouping_value_hash: 4674e386986c
    trace_names: [support-agent]
    enabled: true
    envelope:
      metrics:
        step_count: gating
        tool_call_count: gating
        latency_ms: report_only
        cost_tokens: report_only
```

One unattended cycle is now:

```bash
maida-heal watch --once
maida-heal status --json
tail -n 5 .maida-heal/events.jsonl
```

For each enabled stream, watch overlaps the persisted absolute cursor, deduplicates
run IDs, rebuilds the recent window, asks Maida for structural verdicts, and updates
the cursor only after success. A failure in one stream degrades that stream in
`status --json` without stopping the others.

When the repeated lookup exceeds the generated envelopes, the structural finding
resembles:

```json
{
  "id": "mh-20260811-0123456789",
  "stream_id": "support-agent-4674e386",
  "status": "open",
  "source": "shadow_watch",
  "metric_failures": [
    {"metric": "step_count", "verdict": "fail", "run_ids": ["<run-id>"]},
    {"metric": "tool_call_count", "verdict": "fail", "run_ids": ["<run-id>"]}
  ]
}
```

The persisted schema contains decision rules and evidence pointers as well. It has
no field for prompts, responses, tool arguments, or tool results. The corresponding
`finding.opened` event carries the same payload-free structural summary.

## 4. Authorize an unattended proposal

The complete next profile is
[`02-propose.yaml`](support-agent/config/02-propose.yaml). It adds only the connected
repository, the writer, path bounds, and GitHub event surface:

```yaml
mode: propose
fixes:
  repo: maida-ai/support-agent-config
  repo_local_path: /srv/support-agent-config
  fixer: command
  command: [python, scripts/fix_retry_budget.py]
  auto_propose: true
  allowed_paths: [prompts/**]
  max_attempts_per_finding: 2
  cooldown_hours: 24
```

The bundled command writer is readable at
[`fix_retry_budget.py`](support-agent/scripts/fix_retry_budget.py). It changes one
line and never judges the result. Replace it with `claude-code` or `api` in a real
configuration; closure stays deterministic regardless of writer kind.

After editing the real config:

```bash
maida-heal config validate
maida-heal config apply
maida-heal watch --once
```

The unattended proposal path is concrete:

1. Reuse or create attempt branch
   `maida-heal/mh-20260811-0123456789-a1` in an isolated worktree.
2. Give the writer structural evidence, metric definitions, relevant git diffs, and
   writable paths. Production trace payloads are absent.
3. Run the writer in the worktree.
4. Diff the worktree after the writer exits. Any protected or unlisted path rejects
   the attempt, even if writer-side restrictions failed.
5. Commit the accepted diff with
   `Maida-Heal-Finding: mh-20260811-0123456789`.
6. Push that branch and open a PR through the customer's existing `gh` identity.
7. Persist `fix.proposed` before delivering it to configured sinks.

For this sample, the entire candidate diff is:

```diff
-max_lookup_attempts: 3
+max_lookup_attempts: 1
```

At `propose`, the PR states that the gate is not enabled. A proposal is not a
verified fix.

## 5. Add deterministic closure

[`03-verify.yaml`](support-agent/config/03-verify.yaml) adds explicit candidate and
holdout commands. Both call the readable
[`run_gate.py`](support-agent/scripts/run_gate.py), which does the following:

1. Runs 30 local support-agent scenarios against the prompt on the candidate branch.
2. Writes traces to an isolated temporary `MAIDA_DATA_DIR`.
3. Executes the public `maida drift` CLI with the promoted baseline and unchanged
   policy.
4. Writes the semver'd Maida JSON report to `{report}`.

The holdout invocation uses separate scenario IDs and the protected holdout manifest.
Neither report is produced by a model. Apply the profile once to scaffold the
reviewable gate artifacts and CI workflow:

```bash
maida-heal config validate
maida-heal config apply
```

On a heal branch, `maida-heal verify FINDING_ID` closes only when all three facts are
true in the same invocation:

```text
specific_metrics  PASS  step_count and tool_call_count recovered
holdouts           PASS  every withheld scenario passed
no_new_failures    PASS  the candidate introduced no other failed metric
```

A green but unrelated patch leaves `specific_metrics` false. A holdout regression
leaves `holdouts` false. The policy, findings, workflow, and holdouts are protected
from every writer.

## 6. Hand verified work to customer automation

[`04-full-handoff.yaml`](support-agent/config/04-full-handoff.yaml) adds an explicit
authorization artifact and a signed webhook. It intentionally omits `auto_merge`:

```yaml
mode: full
activation:
  acknowledged_by: "Operator Name <operator@example.com>"
  date: 2026-08-11
  statement: autonomous-fix-loop-authorized
```

Successful closure now emits the teaching
[`fix.verified` event](support-agent/events/fix.verified.json) with
`release_mode: handoff` and `release_ready: true`. Maida-heal comments on the PR and
stops. It does not merge or deploy.

The customer's next process is explicit too: the reference
[`consume_event.py`](support-agent/automation/consume_event.py) verifies HMAC before
parsing, rejects unsupported schema majors, deduplicates `event_id`, checks every
closure condition, and atomically queues a pending release request in SQLite. See
[Customer-owned handoff](customer-handoff.md) to run it and connect that pending row
to an existing release pipeline.

## 7. Keep watching after release

The customer's pipeline merges and deploys the approved PR. Watch continues from its
persisted production cursor. Recovery over later production traces confirms the
change. Recurrence of the same metric and stream within 48 hours opens a revert PR,
emits `rollback.opened`, and pauses fix dispatch; the revert itself never merges
automatically. [Recurrence response](recurrence-response.md) walks through that
failure path event by event.

Executable check:

```bash
uv run pytest -q tests/test_explicit_walkthrough.py
```
