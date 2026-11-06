import subprocess
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / ".githooks" / "commit-msg"


def _run(tmp_path: Path, msg: str) -> tuple[int, str]:
    f = tmp_path / "MSG"
    f.write_text(msg)
    proc = subprocess.run([str(HOOK), str(f)], capture_output=True, check=False)
    return proc.returncode, f.read_text()


@pytest.mark.parametrize(
    "line",
    [
        "Co-Authored-By: Someone <a@b.c>",
        "co-authored-by: x",
        "Generated with Claude Code",
        "GENERATED WITH CLAUDE",
        "Claude-Session: 123",
    ],
)
def test_strips_forbidden_lines(tmp_path: Path, line: str) -> None:
    code, out = _run(tmp_path, f"feat: thing\n\nbody text\n\n{line}\n")
    assert code == 0
    assert out == "feat: thing\n\nbody text\n"


def test_keeps_normal_message(tmp_path: Path) -> None:
    msg = "fix: handle retries\n\nExplain why.\n"
    assert _run(tmp_path, msg) == (0, msg)


def test_rejects_message_that_becomes_empty(tmp_path: Path) -> None:
    code, _ = _run(tmp_path, "Co-authored-by: x\n")
    assert code == 1
