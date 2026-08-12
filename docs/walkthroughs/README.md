# Headless walkthroughs

Start with the explicit path when evaluating how the loop maps to an existing agent:

- [Support-agent loop: production drift to verified handoff](support-agent-loop.md)
- [Customer-owned handoff: authenticated event to release queue](customer-handoff.md)
- [Recurrence response: deployment regresses again](recurrence-response.md)

These walkthroughs include a readable agent loop, two prompt states, every profile's
complete configuration, the command writer, the repository scenario command, a
versioned event, and the receiving automation. Their executable assets live in
[`support-agent/`](support-agent/).

The compact profile walkthroughs remain useful as operator references. Each starts
with configuration, states the unattended behavior, and names the event customer
automation receives. Nothing reads from a TTY.

- [Offline automated demo](demo.md)
- [Shadow: detect and emit](shadow.md)
- [Propose: dispatch a bounded writer](propose.md)
- [Verify: deterministic closure](verify.md)
- [Full: release handoff](full.md)

[`headless.sh`](headless.sh) is an executable fixture walkthrough: config is generated,
one watch cycle runs, status is scraped, and the JSONL event API is asserted without a
network or terminal. The test suite executes it with stdin closed.
