# maida-heal

> Experimental reference implementation. Interfaces and schemas may change before
> the first stable release.

`maida-heal` attaches a self-healing loop to agents that already produce Langfuse
traces. Maida remains the pre-merge behavioral regression gate. This separate
project uses versioned Maida reports as the deterministic verification primitive
around a replaceable fix writer.

## Prove the loop locally

```bash
pip install maida-heal
maida-heal demo
```

The demo is offline, deterministic, and needs no keys. It imports bundled structural
trace fixtures, detects a real behavioral regression with Maida, applies a canned
patch through the `command` fixer, runs the real Maida gate and holdouts, and emits a
versioned closure report. It never contacts Langfuse, GitHub, or an LLM provider.

## Unlock one tier at a time

### 1. Attach and watch

```bash
maida-heal up
```

`up` reuses the standard `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and
`LANGFUSE_HOST` or `LANGFUSE_BASE_URL` variables already used by Langfuse SDKs. It
discovers agent streams, imports a bounded absolute-time window through
`maida import langfuse`, creates conservative starter policies, and prints the first
drift report immediately.

This tier is shadow mode. It creates findings, but it never edits code, calls an LLM,
blocks a merge, opens a pull request, or changes Langfuse data.

### 2. Propose fixes

```bash
maida-heal enable fixes
```

Connect the git repository that controls agent behavior and choose Claude Code, the
Anthropic API, or a user-supplied command as the fix writer. A finding can then
produce a branch and pull request:

```bash
maida-heal fix mh-20260811-0123456789
```

At this tier every fix waits for human review. A post-hoc diff check rejects protected
or out-of-allowlist edits independently of the chosen fix writer.

### 3. Verify in CI

```bash
maida-heal enable gate
```

This promotes reviewed policy artifacts, creates a private holdout split, and
scaffolds `.github/workflows/maida-heal.yml` in the connected repository. Closure
requires the finding's exact metric to recover, every holdout to pass, and no new
Maida failure anywhere.

For findings detected from production drift, CI replays repository scenarios. Live
production confirmation happens only in the post-merge watch window.

This tier never changes policy to make a patch pass. It does not merge a pull request.

### 4. Close the loop

```bash
maida-heal enable auto-merge
```

This command refuses until the gate is active and a verified finding has already
been merged by a human. Automatic merge is bounded by finding closure, a fresh PR
diff/path check, diff size, a daily budget, and the kill switch. A recurrence after an
automatic merge opens a revert pull request and pauses automatic merge; the revert
is never merged automatically.

Stop all mutations at any time:

```bash
maida-heal pause
```

At tier 3 or 4, the command also mirrors the lock into the connected repository and
sets its `MAIDA_HEAL_PAUSED` GitHub Actions variable through the user's existing
`gh` authentication. `resume` clears the local mirrors and sets that variable false.

## The boundary

- The verifier never uses an LLM. Maida alone produces behavioral verdicts.
- The fix writer is replaceable. It can write a patch; it cannot judge that patch.
- Nothing merges automatically unless tier 4 is explicitly enabled.
- No Maida cloud, accounts, or telemetry are used.
- Langfuse access is read-only. Imported data stays under `.maida-heal/`.
- Raw trace payloads are excluded from findings, fixer prompts, pull request bodies,
  and logs. `maida-heal purge` removes imported trace data.

Use `maida-heal status` to see the active tier, autonomous actions, limits, and the
single next enable step.

## Commands

```text
maida-heal demo
maida-heal up [--yes]
maida-heal enable fixes|gate|auto-merge
maida-heal disable fixes|gate|auto-merge
maida-heal status
maida-heal watch [--once|--interval SECONDS]
maida-heal findings list
maida-heal findings show FINDING_ID
maida-heal fix FINDING_ID [--dry-run] [--fixer claude-code|api|command]
maida-heal verify FINDING_ID
maida-heal pause
maida-heal resume
maida-heal purge
```

`up` and `enable` may ask questions only in a TTY. Watch, fix, verify, and CI paths
never prompt. Progress is written to stderr; requested text or JSON is written to
stdout. Exit codes match Maida: `0` success, `1` gate or closure failure, `2` missing
or invalid input, and `10` internal failure.

## Design and data contracts

- [Closure and holdouts](docs/closure.md)
- [Data handling and retention](docs/data-handling.md)
- [Architecture](docs/architecture.md)
- [Full configuration reference](docs/configuration.md)
- [Versioned schemas](schemas/README.md)
- [Tier walkthroughs](docs/walkthroughs/README.md)

## Development

```bash
uv sync
uv run pytest
uv run pytest --cov
uv run ruff check .
uv run ruff format --check .
uv run mypy .
```

The dependency on `maida-ai==0.5.0` is exact. `maida-heal` consumes only the public
CLI, documented exit codes, and semver'd report JSON; it does not import verifier
internals or modify the core package.
