# Offline automated demo

Operator input is one noninteractive command:

```bash
uv run maida-heal demo
```

The system runs detect, fix, verify, and handoff with bundled fixtures. The first line
is `FIX VERIFIED`; subsequent lines expose `finding.opened`, `fix.proposed`, and
`fix.verified` exactly where customer automation would receive them. The last product
action is the handoff event and PR-comment preview. No merge or deploy occurs.

Executable check:

```bash
uv run pytest -q tests/test_demo.py
```
