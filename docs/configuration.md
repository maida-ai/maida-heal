# Configuration reference

`.maida-heal/config.yaml` is progressive. A real tier-1 file contains only the
Langfuse host, credential source, selected stream definitions, and schema version.
Credentials are environment state, never configuration. Later `enable` commands add
only their own section.

This is the fully populated reference, not a recommended file to copy verbatim:

```yaml
schema_version: 1.0.0
langfuse:
  host: https://cloud.langfuse.com
  credential_source: environment
  metadata_keys: [agent_id, gateway, workflow]
  window_days: 14
streams:
  - id: support-agent-4674e386
    name: support-agent / session:012345
    grouping: session_pattern
    grouping_key: sessionId
    grouping_value_hash: 0123456789ab
    selectors:
      - grouping: session_pattern
        grouping_key: sessionId
        grouping_value_hash: 0123456789ab
    trace_names: [support-agent]
    selected: true
    outlier: false
    status: watching
fixes:
  repo: owner/agent-config
  repo_local_path: /absolute/path/to/agent-config
  fixer: claude-code
  auto_propose: false
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
  command: [./scripts/run-heal-scenarios, --report, "{report}"]
  holdout_command: [./scripts/run-heal-holdouts, --report, "{report}"]
  holdout_fraction: 0.25
auto_merge:
  enabled_at: 2026-08-11T12:00:00Z
  max_diff_lines: 200
  daily_budget: 3
  recurrence_hours: 48
```

Supported command placeholders are `{finding}`, `{report}`, `{baseline}`, `{policy}`,
`{holdout}`, and `{suite}`. Candidate and holdout commands must include `{report}`
and write semver'd Maida report JSON there. No command executed by watch or CI may
prompt.

Unknown keys are errors. Configuration schema 1.0.0 is published in
`schemas/config-1.0.0.schema.json`.
