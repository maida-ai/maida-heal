"""Turn Maida drift reports into versioned, deduplicated structural findings."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from maida_heal.artifacts import TargetArtifacts
from maida_heal.constants import DETECTION_REPORT_SCHEMA_VERSION
from maida_heal.core import MaidaCLI
from maida_heal.models import (
    Actor,
    Finding,
    FindingSource,
    FindingStatus,
    HealConfig,
    HistoryEvent,
    MetricFailure,
    StreamConfig,
)
from maida_heal.state import StateStore, write_json


@dataclass(frozen=True)
class DetectionResult:
    stream_id: str
    verdict: str
    reports: tuple[Path, ...]
    findings_created: tuple[str, ...]
    findings_updated: tuple[str, ...]


def _finding_id(
    *, stream_id: str, metric: str, detected_at: datetime, run_ids: list[str]
) -> str:
    material = "\0".join([stream_id, metric, *sorted(run_ids)])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:10]
    return f"mh-{detected_at.astimezone(timezone.utc):%Y%m%d}-{digest}"


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def failed_metrics(
    report: dict[str, object], *, evidence_pointer: str, target: str | None = None
) -> list[MetricFailure]:
    trials = report.get("trials")
    run_ids = []
    structural_terms: list[str] = []
    if isinstance(trials, list):
        run_ids = [
            item["trace_id"]
            for item in trials
            if isinstance(item, dict)
            and isinstance(item.get("trace_id"), str)
            and len(item["trace_id"]) == 32
        ]
        structural_terms = sorted(
            {
                tool
                for trial in trials
                if isinstance(trial, dict)
                and isinstance(trial.get("structural_signature"), dict)
                for tool in trial["structural_signature"].get("tool_path", [])
                if isinstance(tool, str) and 0 < len(tool) <= 120
            }
        )[:50]
    aggregates = report.get("aggregate_results")
    if not isinstance(aggregates, list):
        return []
    failures: list[MetricFailure] = []
    for item in aggregates:
        if not isinstance(item, dict) or item.get("verdict") != "fail":
            continue
        metric = item.get("check_name")
        kind = item.get("kind")
        rule = item.get("decision_rule")
        if not isinstance(metric, str) or kind not in {
            "invariant",
            "measured",
            "distributional",
            "statistical",
        }:
            continue
        if not isinstance(rule, str):
            continue
        evidence_run_ids = run_ids
        outcomes = item.get("trial_outcomes")
        if (
            isinstance(outcomes, list)
            and len(outcomes) == len(run_ids)
            and all(isinstance(outcome, bool) for outcome in outcomes)
        ):
            harmful_value = kind == "distributional"
            selected_ids = [
                run_id
                for run_id, outcome in zip(run_ids, outcomes, strict=True)
                if outcome is harmful_value
            ]
            if selected_ids:
                evidence_run_ids = selected_ids
        evidence = item.get("evidence")
        structural = evidence if isinstance(evidence, dict) else {}
        failures.append(
            MetricFailure(
                metric=metric,
                target=target,
                kind=kind,
                decision_rule=rule,
                run_ids=evidence_run_ids,
                structural_terms=structural_terms,
                evidence_pointer=evidence_pointer,
                observed=_number(structural.get("observed")),
                prediction_bound=_number(structural.get("prediction_bound")),
                harmful_exceedances=(
                    int(structural["harmful_exceedances"])
                    if isinstance(structural.get("harmful_exceedances"), int)
                    else None
                ),
                violations=(
                    int(structural["violations"])
                    if isinstance(structural.get("violations"), int)
                    else None
                ),
            )
        )
    return failures


def _open_match(
    findings: list[Finding], *, stream_id: str, metric: str
) -> Finding | None:
    active = {
        FindingStatus.OPEN,
        FindingStatus.FIX_PROPOSED,
        FindingStatus.VERIFYING,
        FindingStatus.FIX_REJECTED,
    }
    return next(
        (
            finding
            for finding in reversed(findings)
            if finding.stream_id == stream_id
            and metric in finding.metric_names
            and finding.status in active
        ),
        None,
    )


def persist_failures(
    state: StateStore,
    config: HealConfig,
    stream: StreamConfig,
    failures: list[MetricFailure],
    *,
    source: FindingSource,
    detected_at: datetime,
) -> tuple[list[str], list[str]]:
    created: list[str] = []
    updated: list[str] = []
    findings = state.list_findings(config)
    imported_at = {
        record.trace_id: record.started_at for record in state.load_imports().records
    }
    for failure in failures:
        onset_candidates = [
            imported_at[run_id] for run_id in failure.run_ids if run_id in imported_at
        ]
        onset = min(onset_candidates) if onset_candidates else detected_at
        existing = _open_match(findings, stream_id=stream.id, metric=failure.metric)
        if existing is not None:
            current = existing.metric_failures[0]
            new_run_ids = [
                run_id for run_id in failure.run_ids if run_id not in current.run_ids
            ]
            if not new_run_ids:
                continue
            current.run_ids = [*current.run_ids, *new_run_ids]
            current.evidence_pointer = failure.evidence_pointer
            current.observed = failure.observed
            current.prediction_bound = failure.prediction_bound
            current.harmful_exceedances = failure.harmful_exceedances
            current.violations = failure.violations
            existing.updated_at = detected_at
            existing.onset_at = min(existing.onset_at or onset, onset)
            existing.history.append(
                HistoryEvent(
                    timestamp=detected_at,
                    actor=Actor.SYSTEM,
                    action="evidence_attached",
                    detail=(
                        f"Repeat {failure.metric} failure attached to the open finding."
                    ),
                )
            )
            state.save_finding(existing, config)
            updated.append(existing.id)
            continue
        identifier = _finding_id(
            stream_id=stream.id,
            metric=failure.metric,
            detected_at=detected_at,
            run_ids=failure.run_ids,
        )
        finding = Finding(
            id=identifier,
            stream=stream.name,
            stream_id=stream.id,
            source=source,
            title=f"{failure.metric} drift on {stream.name}",
            summary=(
                f"Maida reported a {failure.kind} failure for {failure.metric}. "
                "The finding contains structural evidence pointers only."
            ),
            detected_at=detected_at,
            onset_at=onset,
            updated_at=detected_at,
            metric_failures=[failure],
            history=[
                HistoryEvent(
                    timestamp=detected_at,
                    actor=Actor.SYSTEM,
                    action="finding_opened",
                    detail=f"Shadow watch detected {failure.metric} drift.",
                )
            ],
        )
        state.save_finding(finding, config)
        findings.append(finding)
        created.append(identifier)
    return created, updated


def evaluate_stream(
    state: StateStore,
    core: MaidaCLI,
    config: HealConfig,
    stream: StreamConfig,
    targets: list[TargetArtifacts],
    *,
    detected_at: datetime,
    source: FindingSource = FindingSource.SHADOW_WATCH,
) -> DetectionResult:
    reports: list[Path] = []
    failures: list[MetricFailure] = []
    verdicts: list[str] = []
    stamp = detected_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for target in targets:
        report_path = (
            state.reports_dir
            / stream.id
            / f"{stamp}-{target.run_name.replace('/', '-')}.json"
        )
        report = core.drift(
            window=target.recent_window,
            baseline=target.baseline,
            policy=target.policy,
            agent=target.run_name,
            report_path=report_path,
        )
        reports.append(report_path)
        verdict = report.get("verdict")
        verdicts.append(verdict if isinstance(verdict, str) else "inconclusive")
        pointer = report_path.relative_to(state.project_root).as_posix()
        failures.extend(
            failed_metrics(report, evidence_pointer=pointer, target=target.run_name)
        )

    created: list[str] = []
    updated: list[str] = []
    if failures:
        created, updated = persist_failures(
            state,
            config,
            stream,
            failures,
            source=source,
            detected_at=detected_at,
        )
    overall = (
        "fail"
        if "fail" in verdicts
        else "inconclusive"
        if "inconclusive" in verdicts or not verdicts
        else "pass"
    )
    summary_path = state.reports_dir / stream.id / f"{stamp}-summary.json"
    write_json(
        summary_path,
        {
            "schema_version": DETECTION_REPORT_SCHEMA_VERSION,
            "stream_id": stream.id,
            "generated_at": detected_at.isoformat(),
            "verdict": overall,
            "reports": [
                path.relative_to(state.project_root).as_posix() for path in reports
            ],
            "finding_ids": [*created, *updated],
        },
    )
    return DetectionResult(
        stream_id=stream.id,
        verdict=overall,
        reports=tuple(reports),
        findings_created=tuple(created),
        findings_updated=tuple(updated),
    )
