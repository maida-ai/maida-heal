"""Replaceable fix writers. None of them participate in verification."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class FixerError(RuntimeError):
    """A fix writer failed to produce an applicable candidate patch."""


class Fixer(Protocol):
    kind: str

    def write(
        self, worktree: Path, prompt: str, *, environment: Mapping[str, str]
    ) -> None:
        """Edit only the supplied worktree. Verification happens elsewhere."""


class CommandFixer:
    kind = "command"

    def __init__(self, command: Sequence[str]) -> None:
        if not command:
            raise ValueError("command fixer requires a command")
        self.command = tuple(command)

    def write(
        self, worktree: Path, prompt: str, *, environment: Mapping[str, str]
    ) -> None:
        prompt_path = worktree / ".maida-heal-fixer-prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        env = dict(environment)
        env["MAIDA_HEAL_PROMPT_FILE"] = str(prompt_path)
        try:
            completed = subprocess.run(
                self.command,
                cwd=worktree,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        finally:
            prompt_path.unlink(missing_ok=True)
        if completed.returncode != 0:
            raise FixerError(
                f"command fixer exited {completed.returncode}; its captured output "
                "was suppressed to keep trace-derived content out of logs"
            )


class ClaudeCodeFixer:
    """Headless Claude Code invocation verified against CLI 2.1.227 flags."""

    kind = "claude-code"

    def __init__(self, executable: str = "claude") -> None:
        self.executable = executable

    def write(
        self, worktree: Path, prompt: str, *, environment: Mapping[str, str]
    ) -> None:
        completed = subprocess.run(
            [
                self.executable,
                "--print",
                "--safe-mode",
                "--no-session-persistence",
                "--permission-mode",
                "acceptEdits",
                "--tools",
                "Read,Edit,Write,Glob,Grep",
                "--allowedTools",
                "Read,Edit,Write,Glob,Grep",
                "--disallowedTools",
                "Bash,WebFetch,WebSearch",
                prompt,
            ],
            cwd=worktree,
            env=dict(environment),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise FixerError(
                f"Claude Code exited {completed.returncode}; its captured output "
                "was suppressed to keep trace-derived content out of logs"
            )


class AnthropicAPIFixer:
    kind = "api"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "claude-sonnet-4-5",
        endpoint: str = "https://api.anthropic.com/v1/messages",
        timeout: float = 120.0,
    ) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for the API fixer")
        self.api_key = api_key
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout

    def write(
        self, worktree: Path, prompt: str, *, environment: Mapping[str, str]
    ) -> None:
        del environment
        body = json.dumps(
            {
                "model": self.model,
                "max_tokens": 8192,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"{prompt}\n\nReturn only a unified diff rooted at the "
                            "repository. Do not use Markdown fences."
                        ),
                    }
                ],
            }
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "x-api-key": self.api_key,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise FixerError(f"Anthropic API returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError) as error:
            raise FixerError("Anthropic API request failed") from error
        try:
            blocks = payload["content"]
            diff = "\n".join(
                block["text"]
                for block in blocks
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ).strip()
        except (KeyError, TypeError) as error:
            raise FixerError("Anthropic API returned an invalid response") from error
        if not diff.startswith("diff --git "):
            raise FixerError("API fixer did not return a unified git diff")
        completed = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=worktree,
            input=diff + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise FixerError(
                f"API fixer patch could not be applied: {completed.stderr.strip()}"
            )


def fixer_from_config(kind: str, command: list[str] | None = None) -> Fixer:
    if kind == "command":
        return CommandFixer(command or [])
    if kind == "claude-code":
        return ClaudeCodeFixer()
    if kind == "api":
        return AnthropicAPIFixer(
            os.environ.get("ANTHROPIC_API_KEY", ""),
            model=os.environ.get("MAIDA_HEAL_ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        )
    raise ValueError(f"unknown fixer kind: {kind}")
