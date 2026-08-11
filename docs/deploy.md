# Unattended deployment

The production process is deliberately small: one `maida-heal watch --interval N`
process under the customer's supervisor, plus periodic `maida-heal status --json`
scrapes. There is no daemon framework, queue service, or HTTP server.

## Runtime inputs

Run from the control directory that owns `.maida-heal/`. Persist that entire directory
across restarts. A `propose` or later profile also needs the connected config repository
at the exact `fixes.repo_local_path` in config.

Supply credentials through the runtime environment or its secret manager:

- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and `LANGFUSE_HOST` for read-only
  trace import.
- The configured webhook secret variables.
- `GH_TOKEN` or a mounted `gh` configuration for PR operations.
- The selected writer credential/tool only in `propose` and later profiles.

Do not put values in `config.yaml`. Run `maida-heal config validate` in the final
runtime image and `maida-heal config apply` during a controlled configuration rollout.

## Reference container

The repository `Dockerfile` includes Python, the locked package, git, and `gh`; it
defaults to a five-minute watch interval. Build it in the customer's build system:

```bash
docker build --tag maida-heal:local .
docker run --rm \
  --name maida-heal \
  --env-file /etc/maida-heal/runtime.env \
  --volume /srv/maida-heal/control:/work \
  --volume /srv/agent-config:/srv/agent-config \
  maida-heal:local
```

The config repository path inside the container must match
`fixes.repo_local_path`. Extend the image with the approved fix-writer binary for
`claude-code` or a custom `command`. For handoff, customer release automation consumes
the signed webhook; no deployment credential belongs in this container.

Use a read/write control volume and config-repository mount. Do not bake
`.maida-heal/`, imported data, `gh` state, or credentials into an image layer.

## systemd example

This unit assumes the package environment is `/opt/maida-heal/.venv`, the control
directory is `/srv/maida-heal/control`, and the service account can write the
connected config repository named in config.

```ini
[Unit]
Description=Maida-heal unattended loop
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=maida-heal
Group=maida-heal
WorkingDirectory=/srv/maida-heal/control
EnvironmentFile=/etc/maida-heal/runtime.env
ExecStartPre=/opt/maida-heal/.venv/bin/maida-heal config validate
ExecStart=/opt/maida-heal/.venv/bin/maida-heal watch --interval 300
Restart=always
RestartSec=10
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/srv/maida-heal/control /srv/agent-config

[Install]
WantedBy=multi-user.target
```

Adjust `ReadWritePaths` to exact validated paths. `watch` responds to normal process
termination between cycles; a forced stop is also safe because cursors, attempt
claims, finding history, and event receipts are durable.

## Health scrape

Run from the same control directory:

```bash
maida-heal status --json
```

The output validates against `schemas/status-1.0.0.schema.json`. Treat top-level
`health: degraded`, any degraded stream, `paused: true`, or a rising
`event_stream.pending` count as actionable. Error messages are structural and
redacted; inspect local reports and structured stderr logs for the named phase.

`watch` emits one JSON object per stderr line. A stream failure does not stop other
streams, and a delivery failure does not stop detection or closure. Supervisors
should restart only on process exit, not on a single logged stream/sink failure.

## Kill-switch runbook

To halt all loop mutation:

```bash
cd /srv/maida-heal/control
maida-heal pause
maida-heal status --json
```

Manual pause writes `.maida-heal/heal.lock`, mirrors `.maida/heal.lock` for verify/full,
and updates the generated workflow's `MAIDA_HEAL_PAUSED` variable through `gh`. It
blocks watch, fix, verify, config apply, purge, and automatic merge. Existing customer
deployments are outside this control boundary.

Before resuming, inspect the lock's timestamp/actor, open findings, pending events,
and any revert PR. Then:

```bash
maida-heal resume
maida-heal status --json
```

A post-merge recurrence creates a scoped `fix_dispatch` lock. Detection and event
delivery continue so the recurrence remains visible, but no new writer or automatic
merge is dispatched. The Actions variable carries `fix_dispatch`, which allows
deterministic verification but blocks automatic merge in CI. `resume` clears that
scope after operator review.

## Restart behavior

Every import uses an absolute `[from, to)` range. Per-stream cursors overlap by the
configured interval and deduplicate repeated traces. A finding transition produces a
stable event ID; local JSONL append is ID-checked and webhooks remain safely
at-least-once. Fix attempts use a persisted claim plus deterministic branch name, so
a restart cannot allocate a second attempt or PR for the same dispatch. A persisted
terminal closure is replayed to the event stream without rerunning verification.

Keep the control volume, config repository, and system clock durable/correct. Losing
the control volume loses cursors and delivery receipts and requires a deliberate new
attachment, not an implicit recovery.
