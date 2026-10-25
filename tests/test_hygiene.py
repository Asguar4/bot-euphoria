"""AC 4, 5, 39: repository hygiene, CLI help, README content."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
PY_FILES = sorted(SRC.rglob("*.py"))


def test_files_found() -> None:
    assert len(PY_FILES) >= 20


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_bom_and_lf(path: Path) -> None:
    """AC5."""
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")


def test_lf_line_endings() -> None:
    """DESIGN section 7 (not a numbered AC)."""
    crlf = [str(p.relative_to(ROOT)) for p in PY_FILES if b"\r\n" in p.read_bytes()]
    assert crlf == []


def test_print_only_in_cli_and_client() -> None:
    """AC5."""
    allowed = {"cli.py", "client.py"}
    offenders = [p.name for p in PY_FILES if re.search(r"(?<![\w.])print\(", p.read_text("utf-8"))]
    assert set(offenders) <= allowed, offenders


@pytest.mark.parametrize("needle", ["/var/log", "/tmp", "mkfifo", "os.path"])
def test_forbidden_references(needle: str) -> None:
    """AC5."""
    hits = [str(p) for p in PY_FILES if needle in p.read_text("utf-8")]
    assert hits == []


@pytest.mark.parametrize("args", [["--help"], ["ctl", "--help"], ["run", "--help"]])
def test_cli_help_exit_zero(args: list[str]) -> None:
    """AC4."""
    r = subprocess.run(
        [sys.executable, "-m", "euphoria", *args],
        cwd=ROOT,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert r.returncode == 0, r.stderr
    assert b"usage" in r.stdout.lower()


def test_readme_documents_required_topics() -> None:
    """AC39."""
    low = (ROOT / "readme.md").read_text("utf-8").lower()
    for needle in [
        "windows",
        "linux",
        "python -m euphoria",
        "ctl load",
        "ctl reload",
        "ctl unload",
        "ctl list",
        "ctl status",
        "ctl ping",
        "ctl help",
        "ctl exit",
        "DB_EAGER_CONNECT",
        "ALLOW_REMOTE",
        "control.json",
        "token",
        "shutdown",
        "taskkill",
        "console window",
    ]:
        assert needle.lower() in low, needle


def test_gitignore() -> None:
    gi = (ROOT / ".gitignore").read_text("utf-8")
    assert ".venv/" in gi
    assert ".run/" in gi
    assert "!tests/fixtures/*.json" in gi
