"""Atomic, local-only persistence for progressive configuration and artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from maida_heal.constants import MAIDA_DIR_NAME, STATE_DIR_NAME
from maida_heal.models import (
    ClosureReport,
    Finding,
    GateManifest,
    HealConfig,
    ImportIndex,
    jsonable,
)


class StateError(ValueError):
    """Local state is missing, malformed, or internally inconsistent."""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise StateError(f"state file not found: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise StateError(f"invalid state file {path}: {error}") from error
    if not isinstance(payload, dict):
        raise StateError(f"state file must contain an object: {path}")
    return payload


def kill_switch_paths(state: StateStore, config: HealConfig) -> tuple[Path, ...]:
    paths = [state.lock_path]
    if config.gate is not None and config.fixes is not None:
        repo = Path(config.fixes.repo_local_path).expanduser().resolve()
        paths.append(repo / MAIDA_DIR_NAME / "heal.lock")
    return tuple(paths)


def write_kill_switch(
    state: StateStore, config: HealConfig, payload: dict[str, Any]
) -> None:
    """Write the control lock and, at tier 3+, its connected-repo mirror."""
    state.initialize()
    for path in kill_switch_paths(state, config):
        write_json(path, payload)


def clear_kill_switch(state: StateStore, config: HealConfig) -> None:
    for path in kill_switch_paths(state, config):
        path.unlink(missing_ok=True)


class StateStore:
    """Own `.maida-heal/` state rooted at a user-selected working directory."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.expanduser().resolve()
        self.root = self.project_root / STATE_DIR_NAME

    @property
    def config_path(self) -> Path:
        return self.root / "config.yaml"

    @property
    def imports_path(self) -> Path:
        return self.root / "imports.json"

    @property
    def data_dir(self) -> Path:
        return self.root / "imported" / "maida"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def reports_dir(self) -> Path:
        return self.root / "reports"

    @property
    def streams_dir(self) -> Path:
        return self.root / "streams"

    @property
    def windows_dir(self) -> Path:
        return self.root / "windows"

    @property
    def lock_path(self) -> Path:
        return self.root / "heal.lock"

    @property
    def local_findings_dir(self) -> Path:
        return self.root / "findings"

    def initialize(self) -> None:
        if self.root.is_symlink():
            raise StateError(
                f"state directory must not be a symbolic link: {self.root}"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.local_findings_dir.mkdir(parents=True, exist_ok=True)
        ignore = self.root / ".gitignore"
        if not ignore.exists():
            _atomic_write(ignore, "*\n!.gitignore\n")

    def load_config(self, *, required: bool = True) -> HealConfig:
        if not self.config_path.exists():
            if required:
                raise StateError(
                    "Maida-heal is not attached here. Run `maida-heal up` first."
                )
            return HealConfig()
        try:
            payload = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
            return HealConfig.model_validate(payload)
        except (OSError, yaml.YAMLError, ValueError) as error:
            raise StateError(f"invalid config {self.config_path}: {error}") from error

    def save_config(self, config: HealConfig) -> None:
        self.initialize()
        payload = jsonable(config, exclude_defaults=True)
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        _atomic_write(self.config_path, text)

    def load_imports(self) -> ImportIndex:
        if not self.imports_path.exists():
            return ImportIndex()
        try:
            return ImportIndex.model_validate(read_json(self.imports_path))
        except ValueError as error:
            raise StateError(
                f"invalid import index {self.imports_path}: {error}"
            ) from error

    def save_imports(self, index: ImportIndex) -> None:
        self.initialize()
        write_json(self.imports_path, jsonable(index))

    def findings_dir(self, config: HealConfig | None = None) -> Path:
        selected = config or self.load_config(required=False)
        if selected.gate is not None and selected.fixes is not None:
            repo = Path(selected.fixes.repo_local_path).expanduser().resolve()
            promoted = repo / MAIDA_DIR_NAME / "findings"
            if promoted.parent.exists():
                return promoted
        return self.local_findings_dir

    def list_findings(self, config: HealConfig | None = None) -> list[Finding]:
        directory = self.findings_dir(config)
        if not directory.exists():
            return []
        findings: list[Finding] = []
        for path in sorted(directory.glob("mh-*.json")):
            try:
                findings.append(Finding.model_validate(read_json(path)))
            except ValueError as error:
                raise StateError(f"invalid finding {path}: {error}") from error
        return sorted(findings, key=lambda item: (item.detected_at, item.id))

    def load_finding(
        self, finding_id: str, config: HealConfig | None = None
    ) -> Finding:
        path = self.findings_dir(config) / f"{finding_id}.json"
        try:
            return Finding.model_validate(read_json(path))
        except ValueError as error:
            raise StateError(f"invalid finding {path}: {error}") from error

    def save_finding(self, finding: Finding, config: HealConfig | None = None) -> Path:
        directory = self.findings_dir(config)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{finding.id}.json"
        write_json(path, jsonable(finding))
        return path

    def save_closure(
        self, report: ClosureReport, config: HealConfig | None = None
    ) -> Path:
        directory = self.findings_dir(config) / "closure"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{report.finding_id}.json"
        write_json(path, jsonable(report))
        return path


def load_gate_manifest(repo_root: Path) -> GateManifest:
    path = repo_root / MAIDA_DIR_NAME / "heal.yaml"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return GateManifest.model_validate(payload)
    except FileNotFoundError as error:
        raise StateError(
            "Maida-heal gate is not enabled in this repository. "
            "Run `maida-heal enable gate`."
        ) from error
    except (OSError, yaml.YAMLError, ValueError) as error:
        raise StateError(f"invalid gate manifest {path}: {error}") from error


def save_gate_manifest(repo_root: Path, manifest: GateManifest) -> Path:
    path = repo_root / MAIDA_DIR_NAME / "heal.yaml"
    text = yaml.safe_dump(jsonable(manifest, exclude_defaults=True), sort_keys=False)
    _atomic_write(path, text)
    return path
