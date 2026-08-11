from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from maida_heal.fixers import (
    AnthropicAPIFixer,
    ClaudeCodeFixer,
    CommandFixer,
    FixerError,
    fixer_from_config,
)


def test_command_fixer_edits_worktree_and_removes_transient_prompt(
    tmp_path: Path,
) -> None:
    script = (
        "from pathlib import Path; "
        "assert Path('.maida-heal-fixer-prompt.txt').read_text() == 'bounded'; "
        "Path('prompt.md').write_text('fixed\\n')"
    )
    fixer = CommandFixer([sys.executable, "-c", script])

    fixer.write(tmp_path, "bounded", environment={"PATH": "/usr/bin"})

    assert (tmp_path / "prompt.md").read_text(encoding="utf-8") == "fixed\n"
    assert not (tmp_path / ".maida-heal-fixer-prompt.txt").exists()


def test_command_fixer_reports_failure_without_leaving_prompt(tmp_path: Path) -> None:
    sentinel = "PII-fixer-stderr-sentinel"
    fixer = CommandFixer(
        [
            sys.executable,
            "-c",
            f"import sys; print('{sentinel}', file=sys.stderr); raise SystemExit(7)",
        ]
    )

    with pytest.raises(FixerError, match="exited 7") as captured:
        fixer.write(tmp_path, "bounded", environment={})

    assert sentinel not in str(captured.value)
    assert not (tmp_path / ".maida-heal-fixer-prompt.txt").exists()


def test_claude_code_fixer_uses_verified_headless_edit_only_flags(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Mapping[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("maida_heal.fixers.subprocess.run", fake_run)
    ClaudeCodeFixer("claude-test").write(
        tmp_path, "fix only this", environment={"PATH": "/bin"}
    )

    command, kwargs = calls[0]
    assert command[0] == "claude-test"
    assert {"--print", "--safe-mode", "--no-session-persistence"} <= set(command)
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert command[command.index("--disallowedTools") + 1] == "Bash,WebFetch,WebSearch"
    assert kwargs["cwd"] == tmp_path


class Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_api_fixer_applies_only_a_returned_unified_diff(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True)
    target = tmp_path / "prompt.md"
    target.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    diff = """diff --git a/prompt.md b/prompt.md
index 4a2f01e..f2c82de 100644
--- a/prompt.md
+++ b/prompt.md
@@ -1 +1 @@
-before
+after
"""
    monkeypatch.setattr(
        "maida_heal.fixers.urlopen",
        lambda *_args, **_kwargs: Response(
            {"content": [{"type": "text", "text": diff}]}
        ),
    )

    AnthropicAPIFixer("test-key").write(tmp_path, "bounded", environment={})

    assert target.read_text(encoding="utf-8") == "after\n"


def test_fixer_factory_fails_closed_on_missing_configuration(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="requires a command"):
        fixer_from_config("command")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        fixer_from_config("api")
    with pytest.raises(ValueError, match="unknown fixer"):
        fixer_from_config("other")
