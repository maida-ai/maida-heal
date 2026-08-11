# Architecture

`maida-heal` is an experimental automated loop around Maida, not a verifier fork.
Production traces originate in a customer's Langfuse project. Agent behavior is
controlled by a separate git repository. Configuration authorizes the loop; versioned
events connect it to customer automation.

```text
Langfuse read API
  -> payload-free stream discovery
  -> maida import langfuse (absolute time bounds)
  -> Maida structural comparison
  -> finding + durable event outbox
  -> replaceable writer in an isolated worktree
  -> post-hoc git boundary -> pull request
  -> unchanged Maida gate + holdouts + exact closure
  -> fix.verified handoff event
  -> customer-owned merge and deployment
```

## Profile boundaries

`shadow` stops after finding emission. It needs no git repository, CI system, or LLM
credential. Each enabled stream has its own baseline and conservative generated
policy. Small-sample and high-variance streams remain non-gating.

`propose` connects the repository that determines behavior. A replaceable writer
edits an isolated worktree. Maida-heal then examines the complete git diff. Protected,
out-of-allowlist, and symbolic-link changes are rejected before publication. The
writer cannot write findings or invoke verification.

`verify` promotes findings into `.maida/findings/` and adds repository-defined
candidate and holdout commands. CI runs both commands without changing policy. The
closure layer only validates and combines Maida verdicts.

`full` marks successful closure as release-ready. Handoff is the default: emit
`fix.verified`, update the PR surface, and stop. If `auto_merge` is also present, the
same closure may enter the configured diff/budget/kill-switch envelope. Both release
variants continue post-merge recurrence detection. A recurrence opens a revert PR,
emits `rollback.opened`, and pauses fix dispatch; reverts are never automatic.

## Durable unattended state

The control directory is `.maida-heal/`:

```text
.maida-heal/
  config.yaml             complete desired behavior
  imports.json            deduplicated trace IDs and per-stream cursors
  health.json             last success or redacted degradation by stream
  findings/               strict finding state before verify promotion
  events/
    outbox/               events persisted before delivery
    receipts/             per-event, per-sink delivery receipts
  events.jsonl            default local event API
  imported/               minimized imported run data
  streams/                generated policy and baseline material
  windows/                derived absolute-time windows
```

State writes use a same-directory temporary file, `fsync`, and atomic replacement.
Import cursors advance only after deduplicated records are persisted. Every cycle
overlaps the cursor by a configured number of seconds, then deduplicates by stream
and trace ID, choosing duplicate reads over gaps.

A fix attempt is claimed in the finding before a writer starts. The deterministic
branch name is `maida-heal/<finding-id>-a<attempt>`. On restart, an existing PR is
reconciled into the claimed attempt; interrupted unpublished work reuses that claim
and branch rather than creating another attempt. Terminal closure persists its report
and finding state before event delivery; a CI retry restores the same event ID without
rerunning the gate. Finding history is authoritative and is replayed into the event
outbox after a stop between the two writes.

JSONL delivery scans existing event IDs before append, covering a stop between append
and receipt persistence. Webhooks are intentionally at-least-once: a receiver may
accept a request just before the process stops, so receivers deduplicate by
`event_id`.

## Failure isolation

Import-record construction and comparison run independently per stream. One stream's
failure records a redacted degradation in `health.json`, emits a structured JSON log,
and leaves that stream's cursor unchanged. Other streams continue. Sink delivery
failures remain in the durable outbox and never change a finding verdict or stop a
cycle.

There is no daemon framework, queue service, or health HTTP server. The deployment
shape is `watch --interval` under a container or systemd plus `status --json`.

## Load-bearing verifier boundary

The verifier never uses an LLM. A fix writer may produce text or a diff, but it never
judges that diff. Behavioral verdicts come only from the pinned `maida` CLI and its
semver'd report JSON. Configuration, finding, event, closure, and status models reject
unknown fields.

The project imports no Maida implementation modules. Its boundary consists of public
commands, their exit codes, public run-list output, and report major version 2.
