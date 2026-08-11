# Finding closure and holdouts

A green check is necessary but not sufficient to close a finding. A patch can make
an unrelated scenario pass, omit the affected metric, or trade the original failure
for a new one. `maida-heal verify FINDING_ID` therefore requires three independent
conditions:

1. Every exact metric named by the finding is present in the candidate Maida report
   and has verdict `pass` at the policy's required confidence.
2. The withheld scenario report has overall verdict `pass` and contains no failed
   aggregate.
3. The full candidate report has overall verdict `pass` and contains no new failed
   aggregate.

All three must pass in the same invocation. Missing metrics, `inconclusive`, malformed
reports, a report-schema major mismatch, or an abnormal scenario-command exit fails
closed. The fixer cannot edit policies, `.maida/**`, workflows, or holdouts; a
post-hoc git diff check enforces that boundary even when writer-side restrictions do
not.

## Holdout construction

When the `verify` profile is applied, a stable SHA-256 ordering chooses the configured
fraction of each baseline sample (default `0.25`). Those run IDs move to
`.maida/holdout/<stream>/<target>/manifest.json`; they are removed before Maida
extracts the promoted training baseline. A target must still retain the pinned
verifier's minimum training sample after the split or config application refuses.

Holdout manifests contain IDs, target identity, and `payloads_included: false`.
Their scenarios are run by the connected repository's holdout command. Holdouts are
not included in fixer prompts or normal finding display.

## Closure report

The result is `.maida/findings/closure/<finding-id>.json`, validated against
`schemas/closure-report-1.0.0.schema.json`. It records each condition, its Boolean
result, and pointers to the candidate and holdout Maida reports. It does not copy
trace payloads.

For a drift-sourced finding, CI replays repository scenarios; it does not replay live
production traffic. The post-merge watch window is the true production confirmation.
A production-replay canary is outside this experiment.

In `verify` mode, a successful report emits `fix.verified` with
`release_mode: verify_only` and `release_ready: false`. In `full` mode, the same
deterministic report is embedded in a release-ready event. Handoff then stops;
customer automation owns merge and deployment. No release mode changes the closure
rule.
