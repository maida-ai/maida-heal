# Tier 0: prove it

Run from this repository after `uv sync`:

```bash
uv run maida-heal demo
```

The first line is `LOOP CLOSED`. The four numbered lines then show a real Maida
failure, the deterministic command-writer patch, real candidate and holdout passes,
and closure. The final line names exactly one optional next step: `maida-heal up`.

Offline check: `uv run pytest -q tests/test_demo.py`.
