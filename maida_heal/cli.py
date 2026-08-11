"""Command-line interface for progressively enabling the self-healing loop."""

from __future__ import annotations

import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from maida_heal.constants import EXIT_INTERNAL, EXIT_NOT_FOUND
from maida_heal.core import CoreCommandError, MaidaCLI, ReportCompatibilityError
from maida_heal.disablement import disable_feature
from maida_heal.discovery import StreamCandidate
from maida_heal.enablement import (
    EnablementError,
    check_gh_auth,
)
from maida_heal.enablement import (
    enable_fixes as configure_fixes,
)
from maida_heal.fixers import FixerError, fixer_from_config
from maida_heal.fixtures import FixtureRun
from maida_heal.gate import (
    GateError,
    GitHubCommenter,
    verify_closure,
)
from maida_heal.gate import (
    enable_gate as scaffold_gate,
)
from maida_heal.gitops import GitError, git_root
from maida_heal.healing import PublishError, propose_fix
from maida_heal.killswitch import KillSwitchSyncError, sync_ci_kill_switch
from maida_heal.langfuse import (
    DEFAULT_METADATA_KEYS,
    HTTPClient,
    LangfuseClient,
    LangfuseCredentials,
    LangfuseError,
    resolve_credentials,
)
from maida_heal.models import jsonable
from maida_heal.onboarding import (
    apply_stream_edits,
    attach,
    fixture_attachment_client,
    purge_imported_data,
    watch_once,
)
from maida_heal.release import (
    ReleaseError,
    enable_auto_merge,
    handle_recurrence,
    refresh_human_merges,
)
from maida_heal.state import (
    StateError,
    StateStore,
    clear_kill_switch,
    write_kill_switch,
)

app = typer.Typer(
    name="maida-heal",
    help="Experimental self-healing loop with deterministic Maida verification.",
    no_args_is_help=True,
    add_completion=False,
)
enable_app = typer.Typer(help="Enable the next independently useful tier.")
findings_app = typer.Typer(help="List and inspect structural drift findings.")
app.add_typer(enable_app, name="enable")
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
            EnablementError,
            GitError,
            GateError,
        ),
    ):
        _fail(str(error), EXIT_NOT_FOUND)
    if isinstance(error, (FixerError, PublishError, KillSwitchSyncError, ReleaseError)):
        _fail(str(error), EXIT_INTERNAL)
    _fail(f"internal error: {type(error).__name__}", EXIT_INTERNAL)


def _parse_pairs(values: list[str], *, option: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{option} requires STREAM=VALUE")
        key, result = value.split("=", 1)
        if not key.strip() or not result.strip():
            raise ValueError(f"{option} requires nonempty STREAM=VALUE")
        parsed[key.strip()] = result.strip()
    return parsed


def _parse_merges(values: list[str]) -> dict[str, list[str]]:
    pairs = _parse_pairs(values, option="--merge")
    return {
        target: [item.strip() for item in sources.split(",") if item.strip()]
        for target, sources in pairs.items()
    }


def _prompt_credentials(root: Path) -> LangfuseCredentials:
    try:
        credentials = resolve_credentials(root)
    except LangfuseError:
        if not sys.stdin.isatty():
            raise
        typer.echo(
            "Langfuse keys were not detected. Find them under "
            "Project settings → API Keys.",
            err=True,
        )
        public = typer.prompt("LANGFUSE_PUBLIC_KEY").strip()
        secret = typer.prompt("LANGFUSE_SECRET_KEY", hide_input=True).strip()
        host = typer.prompt(
            "LANGFUSE_HOST", default="https://cloud.langfuse.com"
        ).strip()
        if not public or not secret or not host:
            raise LangfuseError("Langfuse keys and host must not be empty") from None
        credentials = LangfuseCredentials(public, secret, host.rstrip("/"), "prompt")
    # The pinned public importer is a child process and reads these standard SDK
    # variables itself. This process-local bridge is needed when discovery loaded a
    # local config file; values are never persisted by maida-heal.
    os.environ["LANGFUSE_PUBLIC_KEY"] = credentials.public_key
    os.environ["LANGFUSE_SECRET_KEY"] = credentials.secret_key
    os.environ["LANGFUSE_HOST"] = credentials.host
    return credentials


def _configure_interactively(
    candidates: list[StreamCandidate],
    *,
    yes: bool,
    selected: list[str],
    excluded: list[str],
    rename_values: list[str],
    merge_values: list[str],
) -> list[StreamCandidate]:
    typed = candidates
    typer.echo(f"Discovered {len(typed)} agent stream{'s' if len(typed) != 1 else ''}:")
    for index, item in enumerate(typed, start=1):
        marker = "outlier; excluded by default" if item.outlier else "selected"
        typer.echo(f"  {index}. {item.id} — {item.trace_count} traces — {marker}")

    selected_ids: set[str] | None = set(selected) if selected else None
    renames = _parse_pairs(rename_values, option="--rename")
    merges = _parse_merges(merge_values)
    if not yes and not selected:
        if not sys.stdin.isatty():
            raise ValueError("`maida-heal up` needs a TTY or --yes")
        defaults = ",".join(str(i) for i, item in enumerate(typed, 1) if item.selected)
        answer = typer.prompt(
            "Streams to watch (comma-separated numbers or all)", default=defaults
        ).strip()
        if answer.lower() == "all":
            yes = True
        else:
            try:
                indexes = {
                    int(item.strip()) for item in answer.split(",") if item.strip()
                }
            except ValueError as error:
                raise ValueError(
                    "stream selection must use numbers or `all`"
                ) from error
            if any(index < 1 or index > len(typed) for index in indexes):
                raise ValueError("stream selection contains an unknown number")
            selected_ids = {typed[index - 1].id for index in indexes}
        if typer.confirm("Rename or merge streams before attaching?", default=False):
            rename_text = typer.prompt(
                "Renames as STREAM=NAME (semicolon-separated)", default=""
            ).strip()
            merge_text = typer.prompt(
                "Merges as TARGET=SOURCE1,SOURCE2 (semicolon-separated)", default=""
            ).strip()
            if rename_text:
                renames.update(
                    _parse_pairs(rename_text.split(";"), option="interactive rename")
                )
            if merge_text:
                merges.update(_parse_merges(merge_text.split(";")))
    return apply_stream_edits(
        typed,
        select_all=yes,
        selected_ids=selected_ids,
        excluded_ids=set(excluded),
        renames=renames,
        merges=merges,
    )


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
    typer.echo("LOOP CLOSED — Maida verified the candidate without an LLM")
    typer.echo(
        f"1. DETECT — FAIL → finding {result.finding_id} ({result.detection_report})"
    )
    typer.echo(
        "2. FIX — command writer proposed "
        f"{', '.join(result.changed_paths)} ({result.diff_lines} changed lines)"
    )
    typer.echo("3. VERIFY — PASS → finding metric, full gate, and holdouts passed")
    typer.echo("4. CLOSE — CLOSED → semver'd closure report 1.0.0")
    typer.echo(f"Completed locally in {elapsed:.2f}s; no keys or network calls.")
    typer.echo("Attach this to your real agents: `maida-heal up`")


@app.command()
def up(
    yes: Annotated[
        bool, typer.Option("--yes", help="Select every discovered stream")
    ] = False,
    metadata_key: Annotated[
        list[str] | None,
        typer.Option(
            "--metadata-key",
            help="Candidate metadata grouping key; repeat for multiple keys",
        ),
    ] = None,
    select: Annotated[
        list[str] | None,
        typer.Option(
            "--select", help="Select one inferred stream ID; repeat as needed"
        ),
    ] = None,
    exclude_stream: Annotated[
        list[str] | None,
        typer.Option("--exclude-stream", help="Exclude one inferred stream ID"),
    ] = None,
    rename: Annotated[
        list[str] | None,
        typer.Option("--rename", help="Rename an inferred stream as STREAM=NAME"),
    ] = None,
    merge: Annotated[
        list[str] | None,
        typer.Option("--merge", help="Merge streams as TARGET=SOURCE1,SOURCE2"),
    ] = None,
) -> None:
    """Attach to Langfuse and create an immediate shadow-mode drift report."""
    state = _state()
    try:
        _require_unpaused(state)
        keys = list(dict.fromkeys(metadata_key or DEFAULT_METADATA_KEYS))
        fixture_batch: list[FixtureRun] | None = None
        client: LangfuseClient
        if _fixture_enabled():
            fixture_batch, client = fixture_attachment_client()
            host = "fixture://langfuse"
            credential_source = "fixture"
        else:
            credentials = _prompt_credentials(state.project_root)
            client = HTTPClient(credentials)
            host = credentials.host
            credential_source = credentials.source
        result = attach(
            state,
            MaidaCLI(state),
            client,
            host=host,
            credential_source=credential_source,
            now=_now(),
            metadata_keys=keys,
            configure=lambda candidates: _configure_interactively(
                candidates,
                yes=yes,
                selected=select or [],
                excluded=exclude_stream or [],
                rename_values=rename or [],
                merge_values=merge or [],
            ),
            progress=_progress,
            fixture_batch=fixture_batch,
        )
    except Exception as error:
        _handle_error(error)

    selected_streams = [item for item in result.streams if item.selected]
    typer.echo(
        f"FIRST REPORT — {result.traces} traces across "
        f"{len(selected_streams)} selected streams"
    )
    by_id = {item.stream_id: item for item in result.detections}
    for stream in selected_streams:
        if stream.status == "insufficient-data":
            typer.echo(f"  {stream.name}: INSUFFICIENT DATA — no policy enforced")
        else:
            typer.echo(f"  {stream.name}: {by_id[stream.id].verdict.upper()}")
    command = f"cd {shlex.quote(str(state.project_root))} && maida-heal watch --once"
    typer.echo(f"Schedule it yourself (hourly cron): 0 * * * * {command}")
    typer.echo(
        f"Maida-heal is watching {len(selected_streams)} agent streams. When it "
        "finds drift you'll get a finding. To let it also propose fixes: "
        "`maida-heal enable fixes`."
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


def _require_unpaused(state: StateStore) -> None:
    if state.lock_path.exists():
        raise StateError(
            "Maida-heal is paused. Inspect `.maida-heal/heal.lock`, then run "
            "`maida-heal resume` when it is safe to continue."
        )


def _default_repo() -> str | None:
    try:
        return str(git_root(Path.cwd()))
    except GitError:
        return None


@enable_app.command("fixes")
def enable_fixes_command(
    repo: Annotated[
        str | None,
        typer.Option("--repo", help="Local config-repo path or OWNER/REPO slug"),
    ] = None,
    fixer: Annotated[
        str | None,
        typer.Option("--fixer", help="claude-code, api, or command"),
    ] = None,
    command: Annotated[
        str | None,
        typer.Option("--command", help="Command fixer invocation (shell-split only)"),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Accept detected local defaults")
    ] = False,
) -> None:
    """Connect the behavior repository and one replaceable fix writer."""
    state = _state()
    try:
        _require_unpaused(state)
        config = state.load_config()
        selected_repo = repo or _default_repo()
        if selected_repo is None:
            if not sys.stdin.isatty():
                raise EnablementError("pass --repo in non-interactive mode")
            selected_repo = typer.prompt("Config repository path or OWNER/REPO").strip()
        elif not yes and sys.stdin.isatty() and repo is None:
            selected_repo = typer.prompt(
                "Config repository path or OWNER/REPO", default=selected_repo
            ).strip()
        _progress(
            f"Check: git -C {shlex.quote(selected_repo)} rev-parse --show-toplevel"
        )
        _progress("Check: gh auth status")
        command_args = shlex.split(command) if command else None
        if fixer is None and not yes and sys.stdin.isatty():
            detected = (
                "claude-code"
                if shutil.which("claude")
                else "api"
                if os.environ.get("ANTHROPIC_API_KEY")
                else "command"
            )
            fixer = typer.prompt(
                "Fix writer (claude-code, api, command)", default=detected
            ).strip()
        selected = configure_fixes(
            state,
            config,
            repo_value=selected_repo,
            fixer_kind=fixer,
            command=command_args,
            auth_check=check_gh_auth,
        )
    except Exception as error:
        _handle_error(error)
    assert selected.fixes is not None
    check = (
        "claude --version"
        if selected.fixes.fixer == "claude-code"
        else 'test -n "$ANTHROPIC_API_KEY"'
        if selected.fixes.fixer == "api"
        else shlex.join(selected.fixes.command or [])
    )
    typer.echo(f"Fix writer: {selected.fixes.fixer} (check: {check})")
    typer.echo(
        "Fixes will arrive as pull requests for your review. To have Maida verify "
        "them behaviorally in CI: `maida-heal enable gate`."
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


def _detected_gate_command(repo: Path, name: str) -> list[str] | None:
    executable = repo / ".maida" / name
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return None
    return [
        str(executable),
        "--finding",
        "{finding}",
        "--report",
        "{report}",
        "--baseline",
        "{baseline}",
        "--policy",
        "{policy}",
        "--holdout",
        "{holdout}",
        "--suite",
        "{suite}",
    ]


@enable_app.command("gate")
def enable_gate_command(
    command: Annotated[
        str | None,
        typer.Option(
            "--command",
            help="Candidate scenario command; must write {report}",
        ),
    ] = None,
    holdout_command: Annotated[
        str | None,
        typer.Option(
            "--holdout-command",
            help="Withheld-scenario command; must write {report}",
        ),
    ] = None,
    holdout_fraction: Annotated[
        float,
        typer.Option("--holdout-fraction", min=0.05, max=0.5),
    ] = 0.25,
) -> None:
    """Promote reviewed policies and scaffold deterministic CI closure."""
    state = _state()
    try:
        _require_unpaused(state)
        config = state.load_config()
        if config.fixes is None:
            raise StateError("Run `maida-heal enable fixes` before enabling the gate")
        repo = Path(config.fixes.repo_local_path).resolve()
        gate_args = (
            shlex.split(command)
            if command
            else _detected_gate_command(repo, "heal-gate")
        )
        holdout_args = (
            shlex.split(holdout_command)
            if holdout_command
            else _detected_gate_command(repo, "heal-holdout")
        )
        if gate_args is None:
            if not sys.stdin.isatty():
                raise GateError(
                    "pass --command in non-interactive mode; it must execute the "
                    "repository's normal Maida scenarios and write {report}"
                )
            gate_args = shlex.split(
                typer.prompt("Candidate scenario command (include {report})")
            )
        if holdout_args is None:
            if not sys.stdin.isatty():
                raise GateError(
                    "pass --holdout-command in non-interactive mode; it must run "
                    "withheld scenarios and write {report}"
                )
            holdout_args = shlex.split(
                typer.prompt("Withheld scenario command (include {report})")
            )
        result = scaffold_gate(
            state,
            config,
            MaidaCLI(state),
            command=gate_args,
            holdout_command=holdout_args,
            now=_now(),
            holdout_fraction=holdout_fraction,
        )
    except Exception as error:
        _handle_error(error)
    diff = subprocess.run(
        ["git", "diff", "--", ".maida", ".github/workflows/maida-heal.yml"],
        cwd=result.repository,
        text=True,
        capture_output=True,
        check=False,
    ).stdout
    typer.echo("SCAFFOLD REVIEW DIFF")
    typer.echo(diff or "(new untracked files are listed below)")
    for path in result.files:
        typer.echo(f"  {path.relative_to(result.repository)}")
    typer.echo(
        f"Holdout split: {result.holdout_runs} withheld; "
        f"{result.training_runs} training runs."
    )
    typer.echo(
        "Maida will now verify heal branches in CI. Review and commit the scaffold "
        "before opening fix PRs. Next: `maida-heal enable auto-merge`."
    )


@enable_app.command("auto-merge")
def enable_auto_merge_command(
    yes: Annotated[
        bool, typer.Option("--yes", help="Authorize the displayed safety envelope")
    ] = False,
    max_diff_lines: Annotated[int, typer.Option("--max-diff-lines", min=1)] = 200,
    daily_budget: Annotated[int, typer.Option("--daily-budget", min=1)] = 3,
    recurrence_hours: Annotated[int, typer.Option("--recurrence-hours", min=1)] = 48,
) -> None:
    """Authorize bounded merge only after one human-watched closed loop."""
    state = _state()
    try:
        _require_unpaused(state)
        config = state.load_config()
        refresh_human_merges(state, config)
        typer.echo("AUTONOMOUS BEHAVIOR TO AUTHORIZE")
        typer.echo("  Merge only a Maida-verified, closed finding pull request.")
        typer.echo(f"  Refuse patches over {max_diff_lines} changed lines.")
        typer.echo(f"  Stop after {daily_budget} automatic merges per UTC day.")
        typer.echo(
            f"  Within {recurrence_hours} hours, recurrence opens a revert PR "
            "and pauses all automatic merges. The revert never auto-merges."
        )
        typer.echo("  Any unmet condition leaves the pull request for human review.")
        typer.echo("Kill switch: `maida-heal pause`")
        if not yes:
            if not sys.stdin.isatty():
                raise ValueError("pass --yes in non-interactive mode")
            if not typer.confirm(
                "Enable this exact automatic behavior?", default=False
            ):
                raise ValueError("auto-merge authorization was not confirmed")
        enabled = enable_auto_merge(
            state,
            config,
            now=_now(),
            max_diff_lines=max_diff_lines,
            daily_budget=daily_budget,
            recurrence_hours=recurrence_hours,
        )
    except Exception as error:
        _handle_error(error)
    assert enabled.auto_merge is not None
    typer.echo(
        "Auto-merge is enabled inside the displayed envelope. "
        "Run `maida-heal pause` at any time to stop mutations."
    )


@app.command()
def verify(finding_id: Annotated[str, typer.Argument()]) -> None:
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
    """Import, compare, create findings, and dispatch enabled actions."""
    if once and interval is not None:
        _fail("--once and --interval are mutually exclusive")
    state = _state()
    delay = interval or 3600
    while True:
        try:
            _require_unpaused(state)
            fixture_batch, client = _watch_client(state)
            result = watch_once(
                state,
                MaidaCLI(state),
                client,
                now=_now(),
                progress=_progress,
                fixture_batch=fixture_batch,
            )
            config = state.load_config()
            created_ids = [
                finding_id
                for detection in result.detections
                for finding_id in detection.findings_created
            ]
            for finding_id in created_ids:
                finding = state.load_finding(finding_id, config)
                rollback = handle_recurrence(
                    state,
                    config,
                    finding,
                    now=_now(),
                )
                if rollback.opened:
                    _progress(
                        f"Release — recurrence opened {rollback.pull_request_url}; "
                        "Maida-heal is paused"
                    )
                    continue
                if config.fixes is not None and config.fixes.auto_propose:
                    proposal = propose_fix(
                        state,
                        config,
                        finding_id,
                        now=_now(),
                        dry_run=False,
                    )
                    if proposal.pull_request is not None:
                        _progress(
                            f"Fix — proposed {proposal.pull_request.url} for "
                            f"{finding_id}"
                        )
            typer.echo(
                json.dumps(
                    {
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
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as error:
            _handle_error(error)
        if once:
            return
        time.sleep(delay)


@app.command()
def status() -> None:
    """Show the active tier, autonomous behavior, limits, and one next step."""
    state = _state()
    try:
        config = state.load_config(required=False)
    except Exception as error:
        _handle_error(error)
    paused = state.lock_path.exists()
    watching = len([item for item in config.streams if item.selected])
    typer.echo(f"Tier: {config.tier}")
    typer.echo(f"Paused: {'yes' if paused else 'no'}")
    typer.echo(f"Watching: {watching} streams")
    typer.echo(
        "Autonomous: "
        + (
            "bounded verified merges"
            if config.auto_merge is not None
            else "fix proposals only"
            if config.fixes is not None and config.fixes.auto_propose
            else "findings only"
            if config.langfuse is not None
            else "nothing"
        )
    )
    typer.echo(
        "Not autonomous: "
        + (
            "revert pull requests always require human merge"
            if config.auto_merge is not None
            else "merges and releases"
            if config.gate is not None
            else "verification and merges"
            if config.fixes is not None
            else "fixes, verification, and merges"
            if config.langfuse is not None
            else "all loop actions"
        )
    )
    if config.auto_merge is not None:
        typer.echo(
            f"Limits: {config.auto_merge.max_diff_lines} diff lines; "
            f"{config.auto_merge.daily_budget} merges/day"
        )
    next_step = {
        0: "maida-heal up",
        1: "maida-heal enable fixes",
        2: "maida-heal enable gate",
        3: "maida-heal enable auto-merge",
        4: "none — all tiers enabled",
    }[config.tier]
    typer.echo(f"Next step: {next_step}")


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
    if state.lock_path.exists():
        typer.echo("Maida-heal is already paused.")
        return
    config = state.load_config(required=False)
    write_kill_switch(
        state,
        config,
        {
            "schema_version": "1.0.0",
            "paused_at": _now().isoformat(),
            "actor": getpass.getuser(),
        },
    )
    try:
        sync_ci_kill_switch(config, paused=True)
    except KillSwitchSyncError as error:
        _handle_error(error)
    typer.echo(
        "Maida-heal is paused. No watch, fix, verify, or release action will run."
    )


@app.command()
def resume() -> None:
    """Clear the local kill switch after the operator has reviewed it."""
    state = _state()
    if not state.lock_path.exists():
        typer.echo("Maida-heal is not paused.")
        return
    config = state.load_config(required=False)
    clear_kill_switch(state, config)
    try:
        sync_ci_kill_switch(config, paused=False)
    except KillSwitchSyncError as error:
        _handle_error(error)
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


@app.command("disable")
def disable_command(feature: Annotated[str, typer.Argument()]) -> None:
    """Walk back fixes, gate, or auto-merge without deleting user code."""
    state = _state()
    try:
        config = state.load_config()
        result = disable_feature(state, config, feature)
    except Exception as error:
        _handle_error(error)
    typer.echo(f"Disabled {feature}; current tier: {result.config.tier}.")
    for path in result.removed:
        typer.echo(f"  removed: {path}")
    for path in result.preserved:
        typer.echo(f"  preserved for manual review: {path}")


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
