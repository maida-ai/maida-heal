# Event contract

Events are the machine API between Maida-heal and customer automation. Every event is
a strict JSON object validated by `schemas/event-1.0.0.schema.json`. Unknown envelope
or data fields are rejected.

## Envelope

```json
{
  "schema_version": "1.0.0",
  "event_id": "evt_94bfef6ad12d49149bd45ff46a76f196",
  "type": "fix.verified",
  "occurred_at": "2026-08-11T12:00:00Z",
  "stream_id": "support-agent-4674e386",
  "finding_id": "mh-20260811-0123456789",
  "data": {}
}
```

`schema_version` versions this event envelope and its typed `data`. Consumers should
accept compatible 1.x additions and reject unsupported major versions. `event_id` is
the idempotency key. It is derived from the logical transition rather than a delivery
attempt, so a restart or retry keeps the same ID.

`stream_id` and `finding_id` are present when the event belongs to those entities.
They can be absent for loop-wide pause/resume events.

## Event types

| Type | Emitted when | Important data |
| --- | --- | --- |
| `finding.opened` | A structural metric first fails | stream, metrics, run IDs, evidence pointers |
| `finding.evidence_added` | The same stream+metric fails again | updated run IDs and evidence pointers |
| `fix.proposed` | A bounded patch is published as a PR | attempt, branch, changed paths, diff lines, PR |
| `fix.rejected` | Path enforcement or deterministic closure rejects a patch | attempt, reason, changed paths |
| `fix.verified` | All three closure conditions pass | full closure report, release mode, release-ready flag |
| `fix.expired` | The configured attempt budget is exhausted | attempt count and reason |
| `loop.paused` | An operator pauses the loop or recurrence pauses fix dispatch | actor and scope |
| `loop.resumed` | An operator clears the kill switch | actor and scope |
| `rollback.opened` | A post-merge recurrence opens a revert PR | current/prior finding, branch, revert PR |

`fix.verified` has three release modes:

- `verify_only`: closure passed in the `verify` profile; `release_ready` is false.
- `handoff`: closure passed in `full`; customer release automation may proceed.
- `auto_merge`: closure passed and the separately configured merge envelope will be
  evaluated. Consumers must not assume a merge occurred merely from this event.

The embedded closure report always states the three independent conditions:
`specific_metrics`, `holdouts`, and `no_new_failures`.

## Delivery semantics

State changes are persisted before an event enters the durable outbox. Watch startup
reprojects finding history, so a stop between those writes does not lose an event.
CI closure reruns reuse the persisted closure report and reproject its terminal event
without executing the verifier again.

Delivery is at-least-once. Consumers must store `event_id` before performing a
non-idempotent action and return success for an already-seen ID. Ordering is stable
within one local outbox, but consumers must not require global ordering across watch
and CI runtimes.

The local JSONL sink avoids duplicate lines by scanning IDs before append. It also
flushes and `fsync`s each append. Webhook duplicates can still happen if the receiver
accepts a request and the sender stops before writing its receipt; this is why
consumer deduplication remains mandatory.

## JSONL sink

JSONL is always configured. The default path is `.maida-heal/events.jsonl`, relative
to the active runtime root. Each line is one complete event and can be consumed with
ordinary tail/file-shipping tools.

CI closure runs in the connected repository, so its relative JSONL journal is in that
checkout. Use an absolute path only when every runtime can write the same location.
Use a webhook for the usual cross-runtime handoff.

## Webhook sink

```yaml
events:
  sinks:
    - kind: webhook
      url: https://automation.example.com/maida-heal
      secret_env: MAIDA_HEAL_WEBHOOK_SECRET
      timeout_seconds: 5
      max_attempts: 3
      initial_backoff_seconds: 0.25
```

The secret value is read only from `secret_env`. For the exact request body bytes:

```text
signature = hex(HMAC-SHA256(secret, body))
X-Maida-Heal-Signature: sha256=<signature>
X-Maida-Heal-Event-ID: <event_id>
Content-Type: application/json
```

Verify the signature before parsing or acting. Compare it in constant time. A 2xx
response records delivery. Other responses and transport errors retry with bounded
exponential backoff. Exhaustion writes a redacted error log and leaves the event
pending for a later cycle; it never changes a verdict or blocks the loop.

When `config apply` creates the verification workflow, each configured `secret_env`
is mapped to a same-named GitHub Actions secret reference. Operators must create that
secret in the connected repository through their normal secret-management process.

## GitHub sink

GitHub is a domain surface rather than a second generic POST: `fix.proposed` appears
as a PR, verification appears as a PR comment/check, and rollback appears as a revert
PR. The generic dispatcher records a receipt after those domain operations so restart
does not duplicate comments. GitHub access uses the customer's existing `gh`
authentication.

## Data boundary

Events contain structural metrics, run IDs, changed paths, PR pointers, and report
pointers. They never contain prompts, responses, tool arguments/results, trace input
or output, credentials, or raw metadata values. The event schema deliberately has no
payload-shaped extension slot.
