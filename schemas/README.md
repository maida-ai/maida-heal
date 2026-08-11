# Versioned schemas

These Draft 2020-12 JSON Schemas are generated from strict Pydantic models and ship
inside the Python wheel:

- `finding-1.0.0.schema.json`
- `closure-report-1.0.0.schema.json`
- `config-1.0.0.schema.json`

Every corresponding artifact also contains `schema_version: 1.0.0`. Unknown fields
are rejected. Maida report JSON has its own independent semantic version; this
release accepts report major 2 and refuses every other major before changing finding
state.

Schema changes that remove or reinterpret a field require a new major version.
Compatible optional additions require a minor version. Documentation-only or schema
clarifications use a patch version.
