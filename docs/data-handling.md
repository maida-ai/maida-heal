# Data handling

Langfuse access is read-only. `maida-heal` validates credentials with a minimal
observation request and discovers streams using structural, metadata, timing, usage,
and trace-context fields. Discovery does not request input or output fields. Raw
session and selected metadata values are hashed before configuration is persisted.
Credentials are never written to `.maida-heal/config.yaml`.

## Full trace import

The pinned `maida-ai==0.5.0` importer currently requests Langfuse's `io` field while
normalizing a complete trace. `maida-heal` runs it with Maida redaction enabled, an
explicit sensitive-key list, and a small field-size limit. This is stricter storage
than a default Maida import, but it is not metadata-only retrieval. A future pinned
Maida release needs a public structural-only import option before this experiment
can avoid fetching those fields entirely.

Imported native runs live under `.maida-heal/imported/maida/runs/`. Derived hard-link
windows live under `.maida-heal/windows/`; they do not duplicate file contents.
Structural reports, generated policies, and findings live in adjacent directories.
The state directory is created with owner-only permissions (`0700`) and includes a
gitignore that excludes everything except itself, so trace data is not staged
accidentally.

Findings, fixer prompts, PR bodies, closure reports, and command output contain run
IDs and structural summaries only. They have no raw-payload field. Fix-writer process
environments also exclude Langfuse credentials.

## Retention and purge

There is no automatic retention timer in this MVP. Imported runs remain on the local
disk until the operator removes them. Run this from the directory where `up` was
executed:

```bash
maida-heal purge
```

`purge` deletes imported native-run files and derived windows. It preserves findings,
generated policies, and structural reports so the decision history remains usable.
The command refuses while the kill switch is active; resume deliberately, purge, and
pause again if emergency data deletion is required.

No telemetry, account, Maida cloud service, or implicit upload is used. Network
access is limited by tier to the user's Langfuse read API, the selected fix-writer
provider, and GitHub through the user's `gh` authentication. At tier 3 or 4,
`pause` and `resume` set the `MAIDA_HEAL_PAUSED` Actions variable so the local kill
switch also stops or releases the generated remote workflow.
