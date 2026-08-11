# Tier 1: attach and watch

For a real project, export the same Langfuse variables the SDK already uses and run:

```bash
maida-heal up
```

Review inferred streams, then let the command print its immediate first report. It
does not install the displayed schedule. Continue in the foreground with
`maida-heal watch`, or run one scheduler cycle with `maida-heal watch --once`.

The development fixture runs the same local orchestration without a network:

```bash
MAIDA_HEAL_LANGFUSE_FIXTURE=1 \
MAIDA_HEAL_FIXTURE_NOW=2026-08-11T12:00:00Z \
uv run maida-heal up --yes
```

Offline check: `uv run pytest -q tests/test_cli_tier1.py tests/test_onboarding.py`.
