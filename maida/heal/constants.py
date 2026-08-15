"""Stable local paths, schema versions, and process contracts."""

from __future__ import annotations

from pathlib import Path

STATE_DIR_NAME = ".maida-heal"
MAIDA_DIR_NAME = ".maida"
CONFIG_SCHEMA_VERSION = "2.0.0"
FINDING_SCHEMA_VERSION = "1.0.0"
CLOSURE_SCHEMA_VERSION = "1.0.0"
EVENT_SCHEMA_VERSION = "1.0.0"
STATUS_SCHEMA_VERSION = "1.0.0"
IMPORT_INDEX_SCHEMA_VERSION = "1.0.0"
DETECTION_REPORT_SCHEMA_VERSION = "1.0.0"
GATE_MANIFEST_SCHEMA_VERSION = "2.0.0"
SUPPORTED_MAIDA_REPORT_MAJOR = 2

EXIT_SUCCESS = 0
EXIT_GATE_FAILED = 1
EXIT_NOT_FOUND = 2
EXIT_INTERNAL = 10

DEFAULT_WINDOW_DAYS = 14
DEFAULT_RECENT_FRACTION = 0.25
DEFAULT_HOLDOUT_FRACTION = 0.25
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_COOLDOWN_HOURS = 24
DEFAULT_MAX_DIFF_LINES = 200
DEFAULT_DAILY_MERGE_BUDGET = 3
DEFAULT_RECURRENCE_HOURS = 48

PROTECTED_PATH_PATTERNS = (
    ".maida/**",
    ".maida-heal/**",
    ".github/workflows/**",
)
DEFAULT_ALLOWED_PATH_PATTERNS = (
    "prompts/**",
    "skills/**",
    "CLAUDE.md",
    "AGENTS.md",
    "*.md",
    "*.yaml",
    "*.yml",
    "*.json",
    "*.toml",
)

PAYLOAD_REDACTION_KEYS = (
    "api_key",
    "authorization",
    "body",
    "content",
    "cookie",
    "input",
    "output",
    "password",
    "prompt",
    "response",
    "secret",
    "token",
    "args",
    "arguments",
    "result",
)


def state_dir(root: Path) -> Path:
    """Return the local control-plane directory for *root*."""
    return root / STATE_DIR_NAME
