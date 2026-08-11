# Propose: dispatch a bounded writer

The operator adds `fixes` and changes `mode`:

```yaml
mode: propose
fixes:
  repo: owner/agent-config
  repo_local_path: /srv/agent-config
  fixer: command
  command: [./scripts/write-maida-fix]
  auto_propose: true
```

Then the configuration rollout runs:

```bash
maida-heal config validate
maida-heal config apply
```

No conversation follows. Watch claims one attempt, invokes the replaceable writer in
an isolated worktree, enforces protected paths after it exits, and publishes a PR.
Customer automation receives `fix.proposed`; a rejected boundary emits
`fix.rejected`. The PR states that deterministic closure is unavailable until the
profile becomes `verify`.

Offline check:

```bash
uv run pytest -q tests/test_healing.py tests/test_data_safety.py
```
