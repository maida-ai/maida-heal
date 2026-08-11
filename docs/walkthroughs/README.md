# Headless walkthroughs

Each walkthrough starts with configuration, states the unattended behavior, and names
the event customer automation receives. Nothing reads from a TTY.

- [Offline automated demo](demo.md)
- [Shadow: detect and emit](shadow.md)
- [Propose: dispatch a bounded writer](propose.md)
- [Verify: deterministic closure](verify.md)
- [Full: release handoff](full.md)

[`headless.sh`](headless.sh) is an executable fixture walkthrough: config is generated,
one watch cycle runs, status is scraped, and the JSONL event API is asserted without a
network or terminal. The test suite executes it with stdin closed.
