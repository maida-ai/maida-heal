# Configuration reference

`.maida-heal/config.yaml` is the complete desired state of the loop. Commands never
ask questions. `maida-heal up` generates a small `shadow` file; operators review and
change that file through their normal configuration workflow.

Validate without contacting external systems, then apply external prerequisites and
repository scaffolding:

```bash
maida-heal config validate
maida-heal config apply
```

Unknown keys are errors. Environment secrets are never configuration values.

## Complete example

This example shows every section, including the optional automatic-merge variant. It
is a reference, not a file to copy without review.

```yaml
schema_version: 2.0.0
mode: full

langfuse:
  host: https://cloud.langfuse.com
  credential_source: environment
  metadata_keys: [agent_id, gateway, workflow]
  window_days: 14
  cursor_overlap_seconds: 300

streams:
  - id: support-agent-4674e386
    name: support-agent
    grouping: metadata
    grouping_key: agent_id
    grouping_value_hash: 0123456789ab
    selectors:
      - grouping: metadata
        grouping_key: agent_id
        grouping_value_hash: 0123456789ab
    trace_names: [support-agent]
    enabled: true
    mode: verify
    envelope:
      coverage: 0.99
      confidence: 0.95
      metrics:
        latency_ms: report_only
        cost_tokens: disabled
    outlier: false
    status: watching

fixes:
  repo: owner/agent-config
  repo_local_path: /srv/agent-config
  fixer: claude-code
  auto_propose: true
  allowed_paths:
    - prompts/**
    - skills/**
    - CLAUDE.md
    - AGENTS.md
    - "*.md"
    - "*.yaml"
    - "*.yml"
    - "*.json"
    - "*.toml"
  max_attempts_per_finding: 2
  cooldown_hours: 24

gate:
  enabled_at: 2026-08-11T12:00:00Z
  manifest_path: .maida/heal.yaml
  command: [./scripts/heal-candidate, --report, "{report}"]
  holdout_command: [./scripts/heal-holdout, --report, "{report}"]
  holdout_fraction: 0.25

activation:
  acknowledged_by: "Operator Name <operator@example.com>"
  date: 2026-08-11
  statement: autonomous-fix-loop-authorized

events:
  sinks:
    - kind: jsonl
      path: .maida-heal/events.jsonl
    - kind: webhook
      url: https://automation.example.com/maida-heal
      secret_env: MAIDA_HEAL_WEBHOOK_SECRET
      timeout_seconds: 5
      max_attempts: 3
      initial_backoff_seconds: 0.25
    - kind: github
      enabled: true

auto_merge:
  enabled_at: 2026-08-11T12:00:00Z
  max_diff_lines: 200
  daily_budget: 3
  recurrence_hours: 48
```

## Profiles and prerequisites

- `shadow` requires `langfuse` and discovered `streams`.
- `propose` additionally requires `fixes`.
- `verify` additionally requires `gate`.
- `full` additionally requires the exact `activation` attestation.
- `auto_merge` is valid only in `full`; omit it for recommended handoff behavior.

A stream can set `mode` lower than the global profile, never higher. This permits a
high-variance stream to stay in `shadow` while other streams propose or verify.
The generated workflow exits without closure for a stream capped below `verify`, and
its fix PR states that review is manual. In a global `full` profile, a stream capped
at `verify` emits `release_mode: verify_only`; handoff, automatic merge, and automatic
recurrence rollback remain disabled for that stream. Setting `enabled: false`
excludes a stream without deleting its history.

For generated envelopes, `coverage` controls the conservative one-sided prediction
bound and `confidence` controls statistical confidence. Each supported metric can be
`gating`, `report_only`, or `disabled`. Suggested invariants remain commented out
until a person explicitly promotes them.

`auto_propose` defaults to `true` in `propose` and later profiles. Set it to `false`
to retain operator-only `maida-heal fix FINDING_ID` replay while keeping the rest of
the profile active.

`fixes.repo` may initially be either an existing local git path or an `OWNER/REPO`
slug. `repo_local_path` is optional at first. During `config apply`, a slug is cloned
to a deterministic directory below `.maida-heal/repositories/`; a local path is
resolved to its git root. The resulting GitHub slug and absolute local path are then
written back to config so subsequent unattended cycles use one exact repository.

## Writers and scenario commands

Writer kinds are `claude-code`, `api`, and `command`. The `command` writer requires a
nonempty argv list. `claude-code` is detected at apply time; the API writer reads
`ANTHROPIC_API_KEY` only from the process environment.

Supported scenario placeholders are `{finding}`, `{report}`, `{baseline}`,
`{policy}`, `{holdout}`, and `{suite}`. Candidate and holdout commands must include
`{report}` and write semver'd Maida report JSON there. These commands cannot prompt.

## Event sinks

JSONL is inserted automatically if omitted and therefore cannot be disabled. Relative
JSONL paths are resolved against the process control root: the directory containing
`.maida-heal/` for watch, and the connected repository checkout for CI verification.
Use a webhook sink when one consumer must receive events from both runtimes.

`secret_env` names an environment variable; it never contains the secret. Applying a
verify/full profile maps configured webhook environment names to same-named GitHub
Actions secrets in the generated workflow.

## Rolling back a profile

There are no `enable` or `disable` flows. Remove the section, lower `mode`, validate,
and apply the config. Existing findings and event history are preserved. Lowering
from `full` stops release-ready handoffs; removing `auto_merge` restores handoff
without disabling closure.

Schema 2.0.0 is published at `schemas/config-2.0.0.schema.json`. Schema 1.0.0 remains
published for artifact history. Non-automatic 1.0.0 files are migrated in memory;
an old automatic-merge file is refused until the new attestation is added explicitly.
