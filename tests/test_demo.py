import socket
import time

from pytest import MonkeyPatch
from typer.testing import CliRunner

from maida.heal.cli import app


def test_demo_closes_the_real_loop_offline_under_sixty_seconds(
    monkeypatch: MonkeyPatch,
) -> None:
    def reject_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the demo attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    started = time.perf_counter()
    result = CliRunner().invoke(app, ["demo"])
    elapsed = time.perf_counter() - started

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].startswith("FIX VERIFIED")
    assert "1. DETECT — FAIL" in result.stdout
    assert "2. FIX — command writer" in result.stdout
    assert "3. VERIFY — PASS" in result.stdout
    assert "4. HANDOFF — fix.verified" in result.stdout
    assert '"type": "finding.opened"' in result.stdout
    assert '"type": "fix.proposed"' in result.stdout
    assert '"type": "fix.verified"' in result.stdout
    assert "PR COMMENT PREVIEW" in result.stdout
    assert "did not merge or deploy" in result.stdout
    assert elapsed < 60
