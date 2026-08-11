# Architecture

`maida-heal` is an experimental loop around Maida, not a verifier fork. Production
traces originate in a user's Langfuse project, while behavior-changing files remain
in a separate git repository.

The tier-1 path is read-only at its external boundary:

```text
Langfuse read API
  -> payload-free stream discovery
  -> maida import langfuse (absolute time bounds)
  -> maida extract + maida drift
  -> structural finding in .maida-heal/findings/
```

No git repository, CI system, or LLM credential is needed for this path. A stream has
its own baseline sample and generated policy. Low-sample and high-variance streams
remain ungated.

Tier 2 connects the repository that determines agent behavior. A replaceable writer
edits an isolated worktree. `maida-heal` then independently examines the complete git
diff. Protected or out-of-allowlist changes are rejected before a branch can be
published. Writable-path symlinks are rejected before execution, and a newly changed
symlink is rejected afterward. The writer cannot write findings and cannot invoke
verification.

Tier 3 promotes findings into `.maida/findings/` and adds a repository-defined
scenario command. CI runs that command twice: once for the complete candidate suite
and once for withheld scenarios. Both commands must write Maida report JSON. The
closure layer only validates and combines those Maida verdicts.

Tier 4 adds a bounded release decision after closure. It can merge the associated PR
only when the finding is closed, the diff is small enough, the daily budget remains,
and the kill switch is clear. A matching post-merge recurrence opens a revert PR and
pauses the loop. Reverts always wait for a person.

## Load-bearing boundary

The verifier never uses an LLM. A fix writer may produce text or a diff, but it never
judges that diff. Behavioral verdicts come only from the pinned `maida` CLI and its
semver'd report JSON. Configuration, finding, and closure models reject unknown
fields so a new producer cannot silently smuggle a second verdict path into the
artifacts.

The project imports no Maida implementation modules. The boundary consists of public
commands, their exit codes, validated local run data exposed through commands, and
report major version 2.
