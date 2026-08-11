# Shadow: detect and emit

The operator supplies the same read-only Langfuse environment already used by the
agent process, previews discovery, then writes the generated config:

```bash
maida-heal up --plan
maida-heal up
```

The resulting config contains `mode: shadow` plus first-class streams. Change
`enabled`, `name`, selectors, or envelope overrides in config; no confirmation screen
exists. A scheduler runs:

```bash
maida-heal watch --interval 300
```

The system imports absolute time ranges, updates baselines, compares each stream, and
emits `finding.opened` or `finding.evidence_added`. It never calls a writer or touches
git in this profile.

Offline check:

```bash
uv run pytest -q tests/test_cli_shadow.py tests/test_watch_resilience.py
```
