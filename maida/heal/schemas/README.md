# Versioned schemas

These Draft 2020-12 JSON Schemas are generated from strict Pydantic models and ship
inside the Python wheel:

- `config-2.0.0.schema.json` — complete profile configuration
- `event-1.0.0.schema.json` — stable customer-automation API
- `status-1.0.0.schema.json` — `maida-heal status --json`
- `finding-1.0.0.schema.json` — structural finding and lifecycle history
- `closure-report-1.0.0.schema.json` — exact-metric/holdout/no-new-failure result

`config-1.0.0.schema.json` remains as historical documentation for local-state
migration. New files are written as 2.0.0. Old automatic-merge configuration is not
migrated implicitly because full mode now requires an explicit authorization
attestation.

Each artifact contains its own `schema_version`. Unknown fields are rejected. Maida
report JSON has a separate semantic version; this release accepts report major 2 and
refuses every other major before changing finding state.

Schema changes that remove or reinterpret a field require a new major version.
Compatible optional additions require a minor version. Documentation-only or schema
clarifications use a patch version.

Regenerate and validate checked-in files with:

```bash
uv run python scripts/render_schemas.py
uv run pytest -q tests/test_repository_contracts.py tests/test_events.py
```
