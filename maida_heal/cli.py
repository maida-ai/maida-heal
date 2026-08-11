"""Headless operator CLI for the configuration-driven self-healing loop."""

from __future__ import annotations

import getpass
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from maida_heal.constants import EXIT_INTERNAL, EXIT_NOT_FOUND
from maida_heal.core import CoreCommandError, MaidaCLI, ReportCompatibilityError
from maida_heal.events import EventJournal
from maida_heal.fixers import FixerError, fixer_from_config
from maida_heal.fixtures import FixtureRun
from maida_heal.gate import (
    GateError,
    GitHubCommenter,
    VerificationNotEnabled,
    verify_closure,
)
from maida_heal.gate import (
    enable_gate as scaffold_gate,
)
from maida_heal.gitops import GitError
from maida_heal.healing import PublishError, expire_exhausted_finding, propose_fix
from maida_heal.killswitch import KillSwitchSyncError, sync_ci_kill_switch
from maida_heal.langfuse import (
    DEFAULT_METADATA_KEYS,
    HTTPClient,
    LangfuseClient,
    LangfuseCredentials,
    LangfuseError,
    resolve_credentials,
)
from maida_heal.models import (
    EventEnvelope,
    EventType,
    JsonlSinkConfig,
    LoopMode,
    StatusReport,
    StreamHealth,
    jsonable,
)
from maida_heal.onboarding import (
    attach,
    fixture_attachment_client,
    plan_attachment,
    purge_imported_data,
    watch_once,
)
from maida_heal.prerequisites import (
    PrerequisiteError,
    check_fixer,
    check_gh_auth,
    materialize_config_repo,
)
from maida_heal.release import (
    ReleaseError,
    handle_recurrence,
    refresh_human_merges,
)
from maida_heal.state import (
    StateError,
    StateStore,
    clear_kill_switch,
    read_json,
    write_kill_switch,
)
from maida_heal.structured_log import StructuredLogger

app = typer.Typer(
    name="maida-heal",
    help="Experimental self-healing loop with deterministic Maida verification.",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="Validate or apply the declarative loop configuration.")
findings_app = typer.Typer(help="List and inspect structural drift findings.")
app.add_typer(config_app, name="config")
app.add_typer(findings_app, name="findings")


def _state() -> StateStore:
    return StateStore(Path.cwd())


def _progress(message: str) -> None:
    typer.echo(message, err=True)


def _fail(message: str, code: int = EXIT_NOT_FOUND) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def _handle_error(error: Exception) -> NoReturn:
    if isinstance(error, CoreCommandError):
        code = error.result.returncode
        if code not in {1, 2, 10}:
            code = EXIT_INTERNAL
        _fail(str(error), code)
    if isinstance(
        error,
        (
            StateError,
            ValueError,
            LangfuseError,
            ReportCompatibilityError,
            PrerequisiteError,
            GitError,
            GateError,
        ),
    ):
        _fail(str(error), EXIT_NOT_FOUND)
    if isinstance(error, (FixerError, PublishError, KillSwitchSyncError, ReleaseError)):
        _fail(str(error), EXIT_INTERNAL)
    _fail(f"internal error: {type(error).__name__}", EXIT_INTERNAL)


def _resolve_and_bridge_credentials(root: Path) -> LangfuseCredentials:
    """Resolve existing Langfuse credentials without any terminal interaction."""
    credentials = resolve_credentials(root)
    # The pinned public importer is a child process and reads these standard SDK
    # variables itself. This process-local bridge is needed when discovery loaded a
    # local config file; values are never persisted by maida-heal.
    os.environ["LANGFUSE_PUBLIC_KEY"] = credentials.public_key
    os.environ["LANGFUSE_SECRET_KEY"] = credentials.secret_key
    os.environ["LANGFUSE_HOST"] = credentials.host
    return credentials


def _fixture_enabled() -> bool:
    return os.environ.get("MAIDA_HEAL_LANGFUSE_FIXTURE", "").lower() in {
        "1",
        "true",
        "yes",
    }


def _now() -> datetime:
    override = os.environ.get("MAIDA_HEAL_FIXTURE_NOW", "").strip()
    if override and _fixture_enabled():
        parsed = datetime.fromisoformat(override.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("MAIDA_HEAL_FIXTURE_NOW must include a timezone")
        return parsed
    return datetime.now(timezone.utc)


@app.command()
def demo() -> None:
    """Run the complete offline loop with deterministic fixtures and a command fixer."""
    from maida_heal.demo import run_demo

    started = time.perf_counter()
    try:
        result = run_demo()
    except Exception as error:
        _handle_error(error)
    elapsed = time.perf_counter() - started
    if result.closure.verdict != "closed":
        _fail("the bundled deterministic closure did not pass", 1)
    typer.echo("FIX VERIFIED — handoff ready; Maida did not merge or deploy")
    typer.echo(
        f"1. DETECT — FAIL → finding {result.finding_id} ({result.detection_report})"
    )
    typer.echo(
        "2. FIX — command writer proposed "
        f"{', '.join(result.changed_paths)} ({result.diff_lines} changed lines)"
    )
    typer.echo("3. VERIFY — PASS → finding metric, full gate, and holdouts passed")
    typer.echo("4. HANDOFF — fix.verified → customer release automation may proceed")
    for event in result.events:
        typer.echo(
            "EVENT — your automation would receive this event here: "
            + json.dumps(event, ensure_ascii=False, sort_keys=True)
        )
    typer.echo("PR COMMENT PREVIEW")
    typer.echo(result.pr_comment.rstrip())
    typer.echo(f"Completed locally in {elapsed:.2f}s; no keys or network calls.")
    typer.echo(
        "No merge or deploy occurred. Bootstrap a shadow profile: `maida-heal up`"
    )


@app.command()
def up(
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Discover and print writes without changing state"),
    ] = False,
    metadata_key: Annotated[
        list[str] | None,
        typer.Option(
            "--metadata-key",
            help="Candidate metadata grouping key; repeat for multiple keys",
        ),
    ] = None,
) -> None:
    """Discover streams and write a complete non-interactive shadow profile."""
    state = _state()
    try:
        if not plan:
            _require_unpaused(state)
            if state.config_path.exists():
                raise StateError(
                    "Maida-heal is already configured here; `up` is bootstrap-only. "
                    "Edit `.maida-heal/config.yaml` or use `up --plan` to inspect "
                    "current discovery defaults."
                )
        keys = list(dict.fromkeys(metadata_key or DEFAULT_METADATA_KEYS))
        fixture_batch: list[FixtureRun] | None = None
        client: LangfuseClient
        if _fixture_enabled():
            fixture_batch, client = fixture_attachment_client()
            host = "fixture://langfuse"
            credential_source = "fixture"
        else:
            credentials = _resolve_and_bridge_credentials(state.project_root)
            client = HTTPClient(credentials)
            host = credentials.host
            credential_source = credentials.source
        if plan:
            proposed = plan_attachment(
                state,
                client,
                now=_now(),
                metadata_keys=keys,
            )
            typer.echo(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "plan": True,
                        "mode": "shadow",
                        "window": {
                            "from": proposed.window_start.isoformat(),
                            "to": proposed.window_end.isoformat(),
                        },
                        "traces": proposed.traces,
                        "streams": [
                            {
                                "id": stream.id,
                                "name": stream.name,
                                "enabled": stream.enabled,
                                "outlier": stream.outlier,
                            }
                            for stream in proposed.streams
                        ],
                        "writes": list(proposed.writes),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        result = attach(
            state,
            MaidaCLI(state),
            client,
            host=host,
            credential_source=credential_source,
            now=_now(),
            metadata_keys=keys,
            configure=None,
            progress=_progress,
            fixture_batch=fixture_batch,
        )
        config = state.load_config()
        state.reconcile_finding_events(config)
        EventJournal(state.project_root, config.events).flush()
    except Exception as error:
        _handle_error(error)

    selected_streams = [item for item in result.streams if item.enabled]
    by_id = {item.stream_id: item for item in result.detections}
    typer.echo(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "mode": "shadow",
                "window": {
                    "from": result.window_start.isoformat(),
                    "to": result.window_end.isoformat(),
                },
                "traces": result.traces,
                "streams": len(selected_streams),
                "reports": [
                    {
                        "stream_id": stream.id,
                        "status": stream.status,
                        "verdict": (
                            by_id[stream.id].verdict
                            if stream.id in by_id
                            else "insufficient-data"
                        ),
                    }
                    for stream in selected_streams
                ],
                "errors": [
                    {
                        "stream_id": item.stream_id,
                        "phase": item.phase,
                        "error_code": item.error_code,
                    }
                    for item in result.errors
                ],
                "next": (
                    "edit .maida-heal/config.yaml, then run maida-heal config validate"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _watch_client(state: StateStore) -> tuple[list[FixtureRun] | None, LangfuseClient]:
    config = state.load_config()
    if _fixture_enabled():
        return fixture_attachment_client()
    credentials = resolve_credentials(state.project_root)
    if config.langfuse is not None and credentials.host != config.langfuse.host:
        credentials = LangfuseCredentials(
            credentials.public_key,
            credentials.secret_key,
            config.langfuse.host,
            credentials.source,
        )
    os.environ["LANGFUSE_PUBLIC_KEY"] = credentials.public_key
    os.environ["LANGFUSE_SECRET_KEY"] = credentials.secret_key
    os.environ["LANGFUSE_HOST"] = credentials.host
    return None, HTTPClient(credentials)


def _pause_scope(state: StateStore) -> str | None:
    if not state.lock_path.exists():
        return None
    try:
        payload = read_json(state.lock_path)
    except StateError:
        return "all"
    scope = payload.get("scope")
    return scope if scope in {"all", "fix_dispatch"} else "all"


def _require_unpaused(state: StateStore, *, operation: str = "all") -> None:
    scope = _pause_scope(state)
    if scope == "all" or (scope == "fix_dispatch" and operation == "fix_dispatch"):
        raise StateError(
            "Maida-heal is paused. Inspect `.maida-heal/heal.lock`, then run "
            "`maida-heal resume` when it is safe to continue."
        )


def _status_payload(state: StateStore) -> dict[str, object]:
    config = state.load_config(required=False)
    health = state.load_health()
    scope = _pause_scope(state)
    configured = config.langfuse is not None
    overall = (
        "paused"
        if scope == "all"
        else "degraded"
        if health.status == "degraded" or scope == "fix_dispatch"
        else "healthy"
        if health.status == "healthy"
        else "not_configured"
        if not configured
        else "unknown"
    )
    jsonl = next(
        item for item in config.events.sinks if isinstance(item, JsonlSinkConfig)
    )
    journal = EventJournal(state.project_root, config.events)
    streams: list[dict[str, object]] = []
    for stream in config.streams:
        current = health.streams.get(stream.id, StreamHealth(stream_id=stream.id))
        streams.append(
            {
                "id": stream.id,
                "name": stream.name,
                "enabled": stream.enabled,
                "effective_mode": config.effective_mode(stream).value,
                "health": current.status,
                "last_success_at": (
                    current.last_success_at.isoformat()
                    if current.last_success_at is not None
                    else None
                ),
                "error": (
                    {
                        "code": current.error_code,
                        "message": current.error_message,
                    }
                    if current.status == "degraded"
                    else None
                ),
            }
        )
    report = StatusReport.model_validate(
        {
            "schema_version": "1.0.0",
            "mode": config.mode.value,
            "health": overall,
            "paused": scope is not None,
            "pause_scope": scope,
            "updated_at": health.updated_at.isoformat(),
            "autonomy": {
                "detect": configured,
                "propose": config.mode.allows(LoopMode.PROPOSE),
                "verify": config.mode.allows(LoopMode.VERIFY),
                "release": config.release_mode,
            },
            "limits": (
                {
                    "max_diff_lines": config.auto_merge.max_diff_lines,
                    "daily_merge_budget": config.auto_merge.daily_budget,
                    "recurrence_hours": config.auto_merge.recurrence_hours,
                }
                if config.auto_merge is not None
                else None
            ),
            "event_stream": {
                "format": "jsonl",
                "path": jsonl.path,
                "pending": journal.pending_count(),
            },
            "streams": streams,
        }
    )
    return jsonable(report)


@config_app.command("validate")
def config_validate() -> None:
    """Validate the complete config profile without contacting external systems."""
    state = _state()
    try:
        config = state.load_config()
    except Exception as error:
        _handle_error(error)
    typer.echo(
        json.dumps(
            {
                "schema_version": config.schema_version,
                "valid": True,
                "mode": config.mode.value,
                "streams": len([item for item in config.streams if item.enabled]),
                "release": config.release_mode,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@config_app.command("apply")
def config_apply() -> None:
    """Validate external prerequisites and synchronize config-derived scaffolding."""
    state = _state()
    files: list[str] = []
    holdouts = 0
    training = 0
    try:
        _require_unpaused(state)
        config = state.load_config()
        if config.mode.allows(LoopMode.PROPOSE):
            assert config.fixes is not None
            check_gh_auth()
            materialize_config_repo(state, config.fixes)
            check_fixer(config.fixes.fixer, config.fixes.command)
            state.save_config(config)
        if config.mode.allows(LoopMode.VERIFY):
            assert config.gate is not None
            holdout_command = config.gate.holdout_command or config.gate.command
            result = scaffold_gate(
                state,
                config,
                MaidaCLI(state),
                command=config.gate.command,
                holdout_command=holdout_command,
                now=_now(),
                holdout_fraction=config.gate.holdout_fraction,
            )
            files = [
                path.relative_to(result.repository).as_posix() for path in result.files
            ]
            holdouts = result.holdout_runs
            training = result.training_runs
    except Exception as error:
        _handle_error(error)
    typer.echo(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "applied": True,
                "mode": config.mode.value,
                "files": files,
                "holdout_runs": holdouts,
                "training_runs": training,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def fix(
    finding_id: Annotated[str, typer.Argument()],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the candidate diff without a PR")
    ] = False,
    fixer: Annotated[
        str | None,
        typer.Option("--fixer", help="Override with claude-code, api, or command"),
    ] = None,
) -> None:
    """Ask a replaceable writer for one bounded candidate patch."""
    state = _state()
    try:
        config = state.load_config()
        selected_fixer = None
        if fixer is not None:
            command_args = config.fixes.command if config.fixes is not None else None
            selected_fixer = fixer_from_config(fixer, command_args)
        result = propose_fix(
            state,
            config,
            finding_id,
            now=_now(),
            dry_run=dry_run,
            fixer=selected_fixer,
        )
    except Exception as error:
        _handle_error(error)
    typer.echo(f"FIX {result.outcome.upper()} — {result.branch}")
    typer.echo(result.inspection.diff)
    if result.rejection:
        typer.echo(f"Rejected: {result.rejection}", err=True)
        if not dry_run:
            raise typer.Exit(1)
    if result.pull_request:
        typer.echo(f"Pull request: {result.pull_request.url}")


@app.command()
def verify(
    finding_id: Annotated[str, typer.Argument()],
    if_enabled: Annotated[
        bool,
        typer.Option(
            "--if-enabled",
            help="Exit successfully without closure for a lower-mode stream",
        ),
    ] = False,
) -> None:
    """Run the repository gate, holdouts, and exact-finding closure rule."""
    try:
        report = verify_closure(
            Path.cwd(),
            finding_id,
            now=_now(),
            commenter=GitHubCommenter()
            if os.environ.get("GITHUB_ACTIONS") == "true"
            else None,
        )
    except VerificationNotEnabled as error:
        if if_enabled:
            typer.echo(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "status": "skipped",
                        "reason": str(error),
                    },
                    ensure_ascii=False,
                )
            )
            return
        _handle_error(error)
    except Exception as error:
        _handle_error(error)
    typer.echo(json.dumps(jsonable(report), ensure_ascii=False, indent=2))
    if report.verdict != "closed":
        raise typer.Exit(1)


@app.command()
def watch(
    once: Annotated[
        bool, typer.Option("--once", help="Run one import/check cycle")
    ] = False,
    interval: Annotated[
        int | None, typer.Option("--interval", min=1, help="Repeat every N seconds")
    ] = None,
) -> None:
    """Run the restart-safe import, compare, event, and fix-dispatch workhorse."""
    if once and interval is not None:
        _fail("--once and --interval are mutually exclusive")
    state = _state()
    delay = interval or 3600
    logger = StructuredLogger()
    while True:
        try:
            config = state.load_config()
            _require_unpaused(state, operation="watch")
            cycle_now = _now()
            logger.write("info", "watch.cycle_started", "Watch cycle started.")
            fixture_batch, client = _watch_client(state)
            result = watch_once(
                state,
                MaidaCLI(state),
                client,
                now=cycle_now,
                progress=lambda message: logger.write(
                    "info", "watch.progress", "Watch phase advanced.", detail=message
                ),
                fixture_batch=fixture_batch,
            )
            config = state.load_config()
            for failure in result.errors:
                logger.write(
                    "error",
                    "watch.stream_failed",
                    failure.message,
                    stream_id=failure.stream_id,
                    phase=failure.phase,
                    error_code=failure.error_code,
                )
            if config.mode is LoopMode.FULL:
                try:
                    refresh_human_merges(state, config)
                except ReleaseError:
                    logger.write(
                        "error",
                        "release.merge_state_unavailable",
                        "Could not refresh merged pull requests; detection continues.",
                    )
            created_ids = [
                finding_id
                for detection in result.detections
                for finding_id in detection.findings_created
            ]
            recurrence_ids = list(
                dict.fromkeys(
                    [
                        *created_ids,
                        *[
                            item.id
                            for item in state.list_findings(config)
                            if item.source.value == "post_merge_watch"
                            and item.status.value == "open"
                        ],
                    ]
                )
            )
            for finding_id in recurrence_ids:
                finding = state.load_finding(finding_id, config)
                rollback = handle_recurrence(
                    state,
                    config,
                    finding,
                    now=cycle_now,
                )
                if rollback.opened:
                    logger.write(
                        "warning",
                        "rollback.opened",
                        "Recurrence opened a revert pull request and paused "
                        "fix dispatch.",
                        finding_id=finding_id,
                        pull_request_url=rollback.pull_request_url,
                    )
            stream_by_id = {item.id: item for item in config.streams}
            dispatch_paused = _pause_scope(state) is not None
            if (
                config.fixes is not None
                and config.fixes.auto_propose
                and not dispatch_paused
            ):
                for finding in state.list_findings(config):
                    stream = stream_by_id.get(finding.stream_id)
                    if stream is None or not stream.enabled:
                        continue
                    if not config.effective_mode(stream).allows(LoopMode.PROPOSE):
                        continue
                    if finding.status.value not in {"open", "fix_rejected"}:
                        continue
                    if (
                        finding.cooldown_until is not None
                        and cycle_now < finding.cooldown_until
                    ):
                        continue
                    running_claim = bool(
                        finding.attempts and finding.attempts[-1].outcome == "running"
                    )
                    if (
                        len(finding.attempts) >= config.fixes.max_attempts_per_finding
                        and not running_claim
                    ):
                        expire_exhausted_finding(state, config, finding, now=cycle_now)
                        continue
                    try:
                        proposal = propose_fix(
                            state,
                            config,
                            finding.id,
                            now=cycle_now,
                            dry_run=False,
                        )
                        if proposal.pull_request is not None:
                            logger.write(
                                "info",
                                "fix.proposed",
                                "Fix pull request proposed.",
                                finding_id=finding.id,
                                pull_request_url=proposal.pull_request.url,
                            )
                    except Exception as error:
                        logger.write(
                            "error",
                            "fix.dispatch_failed",
                            "Fix dispatch failed; other streams and findings continue.",
                            finding_id=finding.id,
                            stream_id=finding.stream_id,
                            error_code=type(error).__name__,
                        )
            state.reconcile_finding_events(config)
            delivery = EventJournal(
                state.project_root,
                config.events,
                log=logger.event_delivery,
            ).flush()
            logger.write(
                "info",
                "watch.cycle_completed",
                "Watch cycle completed.",
                traces=result.traces,
                stream_errors=len(result.errors),
                events_delivered=delivery.delivered,
                event_failures=delivery.failed,
            )
            typer.echo(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "window": {
                            "from": result.window_start.isoformat(),
                            "to": result.window_end.isoformat(),
                        },
                        "traces": result.traces,
                        "streams": [
                            {
                                "stream": item.stream_id,
                                "verdict": item.verdict,
                                "findings_created": list(item.findings_created),
                                "findings_updated": list(item.findings_updated),
                            }
                            for item in result.detections
                        ],
                        "errors": [
                            {
                                "stream_id": item.stream_id,
                                "phase": item.phase,
                                "error_code": item.error_code,
                            }
                            for item in result.errors
                        ],
                        "events": {
                            "delivered": delivery.delivered,
                            "failed": delivery.failed,
                        },
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as error:
            if not state.config_path.exists():
                _handle_error(error)
            if isinstance(error, StateError) and _pause_scope(state) == "all":
                if once:
                    _handle_error(error)
                logger.write(
                    "warning",
                    "watch.paused",
                    "Kill switch is active; no cycle work was performed.",
                )
                typer.echo(
                    json.dumps(
                        {
                            "schema_version": "1.0.0",
                            "status": "paused",
                        }
                    )
                )
                time.sleep(delay)
                continue
            cycle_now = _now()
            try:
                health = state.load_health()
                config = state.load_config()
                health.updated_at = cycle_now
                health.cycle_completed_at = cycle_now
                for stream in config.streams:
                    if not stream.enabled:
                        continue
                    current = health.streams.setdefault(
                        stream.id, StreamHealth(stream_id=stream.id)
                    )
                    current.status = "degraded"
                    current.last_attempt_at = cycle_now
                    current.error_code = type(error).__name__
                    current.error_message = (
                        "watch cycle failed; the next scheduled cycle will retry"
                    )
                state.save_health(health)
            except Exception:
                pass
            logger.write(
                "error",
                "watch.cycle_failed",
                "Watch cycle failed; the next scheduled cycle will retry.",
                error_code=type(error).__name__,
            )
            typer.echo(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "status": "degraded",
                        "error_code": type(error).__name__,
                    }
                )
            )
        if once:
            return
        time.sleep(delay)


@app.command()
def status(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the stable machine-readable status")
    ] = False,
) -> None:
    """Show current health, authorized actions, limits, and event backlog."""
    state = _state()
    try:
        payload = _status_payload(state)
    except Exception as error:
        _handle_error(error)
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    autonomy = payload["autonomy"]
    assert isinstance(autonomy, dict)
    typer.echo(f"Mode: {payload['mode']}")
    typer.echo(f"Health: {payload['health']}")
    typer.echo(f"Paused: {'yes' if payload['paused'] else 'no'}")
    typer.echo(
        "Autonomous: detect={detect}, propose={propose}, verify={verify}, "
        "release={release}".format(**autonomy)
    )
    event_stream = payload["event_stream"]
    assert isinstance(event_stream, dict)
    typer.echo(f"Events: {event_stream['path']} ({event_stream['pending']} pending)")


@findings_app.command("list")
def findings_list() -> None:
    """List findings without trace payload content."""
    state = _state()
    try:
        findings = state.list_findings()
    except Exception as error:
        _handle_error(error)
    if not findings:
        typer.echo("No findings.")
        return
    for item in findings:
        metrics = ",".join(item.metric_names)
        typer.echo(f"{item.id}\t{item.status.value}\t{item.stream}\t{metrics}")


@findings_app.command("show")
def findings_show(finding_id: Annotated[str, typer.Argument()]) -> None:
    """Print one structural finding as versioned JSON."""
    state = _state()
    try:
        finding = state.load_finding(finding_id)
    except Exception as error:
        _handle_error(error)
    typer.echo(json.dumps(jsonable(finding), ensure_ascii=False, indent=2))


@app.command()
def pause() -> None:
    """Write the local kill switch checked by every mutating loop command."""
    state = _state()
    state.initialize()
    existing_scope = _pause_scope(state)
    if existing_scope == "all":
        typer.echo("Maida-heal is already paused.")
        return
    config = state.load_config(required=False)
    now = _now()
    actor = getpass.getuser()
    write_kill_switch(
        state,
        config,
        {
            "schema_version": "1.0.0",
            "paused_at": now.isoformat(),
            "actor": actor,
            "scope": "all",
        },
    )
    try:
        sync_ci_kill_switch(config, paused=True)
    except KillSwitchSyncError as error:
        _handle_error(error)
    event = EventEnvelope.create(
        event_type=EventType.LOOP_PAUSED,
        occurred_at=now,
        dedupe_key=f"manual-pause:{now.isoformat()}:{actor}",
        data={"actor": actor, "scope": "all"},
    )
    journal = EventJournal(state.project_root, config.events)
    journal.queue(event)
    journal.flush()
    typer.echo(
        "Maida-heal is paused. No watch, fix, verify, or release action will run."
    )


@app.command()
def resume() -> None:
    """Clear the local kill switch after the operator has reviewed it."""
    state = _state()
    scope = _pause_scope(state)
    if scope is None:
        typer.echo("Maida-heal is not paused.")
        return
    config = state.load_config(required=False)
    now = _now()
    actor = getpass.getuser()
    try:
        sync_ci_kill_switch(config, paused=False)
    except KillSwitchSyncError as error:
        _handle_error(error)
    clear_kill_switch(state, config)
    event = EventEnvelope.create(
        event_type=EventType.LOOP_RESUMED,
        occurred_at=now,
        dedupe_key=f"resume:{now.isoformat()}:{actor}",
        data={"actor": actor, "scope": scope},
    )
    journal = EventJournal(state.project_root, config.events)
    journal.queue(event)
    journal.flush()
    typer.echo("Maida-heal resumed.")


@app.command()
def purge() -> None:
    """Delete locally imported trace data while preserving structural findings."""
    state = _state()
    try:
        _require_unpaused(state)
        state.load_config()
        removed = purge_imported_data(state)
    except Exception as error:
        _handle_error(error)
    typer.echo(
        f"Purged {removed} imported trace files. Findings and generated structural "
        "artifacts were preserved."
    )


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
