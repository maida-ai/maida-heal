# maida-heal

## Start with the released Maida gate

```bash
uv tool install "maida-ai==0.5.3"
maida demo --regression
```

Expect a deliberate FAIL and PR-comment preview. First-time users should follow the [coding-agent walkthrough](https://maida.ai/docs/getting-started/) to capture one task, review a few checks, and prove pass/fail/repair. This repository is an additional integration or development surface; it is not required for that first gate. Runnable examples and demos live in [maida-tutorials](https://github.com/maida-ai/maida-tutorials).

> Experimental. The package and its machine contracts may change before the first
> stable release.

`maida-heal` runs an unattended self-healing loop around agents that already emit
Langfuse traces. Maida remains the pre-merge behavioral regression gate and the only
verifier. A replaceable fix writer proposes code; customer-owned automation decides
when and how verified code is released.

```text
Customer AI loop
      |
      v
Langfuse traces --read only--> Maida catch --> finding.opened
                                          |
                                          v
                                  replaceable fix writer
                                          |
                                          v
                                 pull request + Maida gate
                                          |
                                          v
                            fix.verified  <--- HANDOFF
                                          |
                                          v
                          customer release automation --> deploy
                                                       --> Customer AI loop
```

Maida stops at the marked handoff by default. It does not sit in the customer's
deployment path.

## Run the automated story offline

```bash
pip install maida-heal
maida-heal demo
```

The deterministic demo needs no keys and makes no network calls. It imports bundled
structural traces, detects a real regression, runs the `command` fix writer, verifies
the exact finding plus holdouts, shows each JSON event handoff, and finishes with a
`fix.verified` event and PR-comment preview. It never merges or deploys.

## Learn with a visible agent loop

The [explicit support-agent walkthrough](docs/walkthroughs/support-agent-loop.md)
starts from readable agent code and a concrete retry regression. It runs both prompt
states, shows the production-import and repository-scenario boundaries, builds each
complete profile, opens the candidate-patch path step by step, and follows
`fix.verified` into a customer-owned, HMAC-authenticated release queue. Separate
walkthroughs cover the [customer handoff](docs/walkthroughs/customer-handoff.md) and
[post-release recurrence](docs/walkthroughs/recurrence-response.md).

## Bootstrap a shadow profile

`up` reuses the standard `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and
`LANGFUSE_HOST` or `LANGFUSE_BASE_URL` environment variables. It has no interactive
path.

```bash
maida-heal up --plan  # discover defaults and print intended writes
maida-heal up         # write config, streams, policies, and the first report
```

The generated `.maida-heal/config.yaml` is the front door. Stream curation happens
there: set `enabled`, rename `name`, combine `selectors`, choose a lower per-stream
`mode`, or override generated envelopes. `up` never changes Langfuse data.

## Configuration profiles

One top-level key controls the loop shape:

```yaml
schema_version: 2.0.0
mode: shadow
```

| Profile | Runs unattended | Does not do |
| --- | --- | --- |
| `shadow` | Imports, compares, and emits findings | Call a fix writer or touch git |
| `propose` | Adds automatic fix dispatch and pull requests | Claim behavioral closure or merge |
| `verify` | Adds the unchanged Maida gate, holdouts, and closure | Mark a patch release-ready or merge |
| `full` | Emits release-ready `fix.verified` handoff events | Merge unless `auto_merge` is separately configured |

Add the section required by a profile, change `mode`, then validate and apply it:

```bash
maida-heal config validate
maida-heal config apply
```

`config apply` checks the configured git/`gh`/writer prerequisites and, for
`verify` or `full`, synchronizes the reviewed `.maida/` artifacts and generated
workflow into the connected repository. There are no `enable` or `disable` command
flows; rolling forward or back is an auditable config diff.

`full` requires an explicit authorization artifact:

```yaml
activation:
  acknowledged_by: "Operator Name <operator@example.com>"
  date: 2026-08-11
  statement: autonomous-fix-loop-authorized
```

Without this exact block, config loading fails before work begins.

## Events are the API

Every state change is a semver'd JSON event. Local JSONL is always active; webhook
and GitHub surfaces can be added together:

```yaml
events:
  sinks:
    - kind: jsonl
      path: .maida-heal/events.jsonl
    - kind: webhook
      url: https://automation.example.com/maida-heal
      secret_env: MAIDA_HEAL_WEBHOOK_SECRET
    - kind: github
```

Webhook bodies are signed with HMAC-SHA256 using the named environment variable.
Delivery is bounded and non-blocking. Events use stable IDs and at-least-once
delivery, so consumers deduplicate by `event_id`. See the complete contract in
[docs/events.md](docs/events.md).

## Handoff first; automatic merge optional

The recommended `full` profile omits `auto_merge`. On closure, Maida-heal marks the
PR verified, emits `fix.verified` with the closure report, and stops. Customer
automation owns merge and deployment.

Adding `auto_merge` is a separate, narrower opt-in. It still requires the `full`
attestation and enforces exact closure, protected paths, a diff cap, a daily budget,
and the kill switch. A matching post-merge recurrence opens a revert PR and pauses
fix dispatch. Revert PRs are never merged automatically.

## Load-bearing boundaries

- The verifier never uses an LLM. Maida alone produces behavioral verdicts.
- The fix writer is replaceable. It writes a candidate; it never judges it.
- Post-hoc path enforcement is authoritative regardless of writer restrictions.
- Handoff mode never merges. Automatic merge is a separate attested config choice.
- No Maida cloud, account, license check, usage reporting, or telemetry is used.
- Langfuse imports are read-only.
- Trace payloads never enter findings, events, writer prompts, PR bodies, or logs.
- Imported data stays under `.maida-heal/`; `maida-heal purge` removes it.

## Operator commands

```text
maida-heal demo
maida-heal up [--plan] [--metadata-key KEY]
maida-heal config validate|apply
maida-heal watch [--once|--interval SECONDS]
maida-heal status [--json]
maida-heal findings list
maida-heal findings show FINDING_ID
maida-heal fix FINDING_ID [--dry-run] [--fixer claude-code|api|command]
maida-heal verify FINDING_ID
maida-heal pause
maida-heal resume
maida-heal purge
```

No command prompts. `watch --interval` is the production workhorse; it writes JSON
logs to stderr and structured requested output to stdout. `status --json` is the
stable health surface. Exit codes match Maida: `0` success, `1` gate or closure
failure, `2` missing/invalid input, and `10` internal failure.

## Design and data contracts

- [Event contract](docs/events.md)
- [Deployment and kill-switch runbook](docs/deploy.md)
- [Configuration reference](docs/configuration.md)
- [Finding closure and holdouts](docs/closure.md)
- [Data handling and retention](docs/data-handling.md)
- [Architecture](docs/architecture.md)
- [Headless walkthroughs](docs/walkthroughs/README.md)
- [Versioned schemas](maida/heal/schemas/README.md)

## Development

```bash
uv sync
uv run pytest
uv run pytest --cov
uv run ruff check .
uv run ruff format --check .
uv run mypy .
```

The dependency on `maida-ai==0.5.0` is exact. `maida-heal` consumes only public
commands, documented exit codes, public local-run commands, and semver'd report JSON.
It does not import verifier internals or modify the core package.
