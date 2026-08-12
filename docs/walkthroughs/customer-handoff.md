# Customer-owned handoff: authenticated event to release queue

Handoff mode ends Maida's responsibility at a verified pull request. This example
shows exactly how customer automation can consume that boundary without letting an
HTTP retry trigger two releases.

The input is the complete versioned
[`fix.verified` event](support-agent/events/fix.verified.json). The receiver is the
standard-library-only
[`consume_event.py`](support-agent/automation/consume_event.py). It is intentionally
a request processor, not a deployment service.

## 1. Configure the sender

The `full` profile names the environment variable containing the shared secret; the
secret itself never enters YAML:

```yaml
events:
  sinks:
    - kind: webhook
      url: https://automation.example.com/maida-heal
      secret_env: MAIDA_HEAL_WEBHOOK_SECRET
```

At delivery, Maida-heal sends the exact JSON bytes with:

```text
X-Maida-Heal-Event-ID: evt_...
X-Maida-Heal-Signature: sha256=<HMAC-SHA256(secret, body)>
```

The same environment name must exist in the watch runtime and verification CI
runtime. A delivery failure remains in the outbox for retry and never changes the
Maida verdict.

## 2. Run the receiver against the fixture event

From the repository root, use a test-only secret to create the same header the sender
would create:

```bash
export MAIDA_HEAL_WEBHOOK_SECRET=test-only-shared-secret
event_file=docs/walkthroughs/support-agent/events/fix.verified.json
signature=$(uv run python -c \
  'import hashlib,hmac,os,sys; body=open(sys.argv[1],"rb").read(); print("sha256="+hmac.new(os.environ["MAIDA_HEAL_WEBHOOK_SECRET"].encode(),body,hashlib.sha256).hexdigest())' \
  "$event_file")
uv run python docs/walkthroughs/support-agent/automation/consume_event.py \
  --body "$event_file" \
  --signature "$signature" \
  --database /tmp/maida-heal-handoff.sqlite3
```

Expected output:

```text
queued release request for mh-20260811-0123456789 from PR #42 using event evt_11111111111111111111111111111111
```

Run the exact command again. The event API is at-least-once, so a duplicate is
normal:

```text
duplicate event evt_11111111111111111111111111111111: already accepted
```

There is still one pending release request, not two.

## 3. Follow the receiver's decision path

The code performs these steps in order:

1. Read `MAIDA_HEAL_WEBHOOK_SECRET` from the process environment.
2. Recompute HMAC-SHA256 over the untouched body bytes and compare in constant time.
3. Parse JSON only after authentication succeeds.
4. Reject event schema majors other than `1`.
5. Insert `event_id` into a SQLite table with a primary-key uniqueness constraint.
6. For `fix.verified`, require `release_mode: handoff`, `release_ready: true`, a
   closed report, and all three passed closure conditions.
7. Insert the event and pending release request in one database transaction.
8. Return success for an already accepted ID so sender retries stop.

An invalid signature never creates the database. A `verify_only` event is recorded
but does not queue release. A `fix.proposed` event is recorded but does not queue
release. This makes event type and profile meaning part of the decision, rather than
treating every green-looking payload as deployable.

## 4. Connect the pending row to the existing release pipeline

The example stops with this customer-owned state:

```text
release_requests
  event_id             evt_1111...
  finding_id           mh-20260811-0123456789
  pull_request_number  42
  status               pending
```

A customer worker can claim that row, re-read PR #42 from its own GitHub trust
boundary, require its normal branch protection and deployment approvals, merge, run
the existing release pipeline, and finally mark the row `released` or `failed`.
Those steps stay customer-specific by design. Maida-heal neither receives deployment
credentials nor sits in that causal chain.

Keep the release worker idempotent too. Use `event_id` as its external idempotency
key, and record the deployment identifier before acknowledging completion. Do not
infer a merge from `fix.verified`; it says closure passed and handoff is ready, not
that a release occurred.

## 5. Test the receiver contract

The repository test uses a bad signature, a valid signature, and a duplicate valid
delivery. It validates the fixture against the published Pydantic event model and
asserts one event row plus one release row:

```bash
uv run pytest -q \
  tests/test_explicit_walkthrough.py::test_customer_handoff_consumer_verifies_hmac_and_deduplicates
```

Executable check:

```bash
uv run pytest -q tests/test_explicit_walkthrough.py
```
