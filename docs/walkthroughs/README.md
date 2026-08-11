# Tier walkthroughs

Each walkthrough starts from the tier below and ends at one independently useful
state. Tier 0 and the fixture form of tier 1 are executed directly by the test suite.
Tiers 2 through 4 are exercised through offline fakes in their linked tests because
the real commands intentionally use the operator's git repository and `gh` session.

- [Tier 0: prove it](tier-0-demo.md)
- [Tier 1: attach and watch](tier-1-shadow.md)
- [Tier 2: propose fixes](tier-2-fixes.md)
- [Tier 3: deterministic closure](tier-3-gate.md)
- [Tier 4: bounded release](tier-4-release.md)
