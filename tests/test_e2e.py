"""End-to-end runs of the real scripts in subprocesses (no Discord, no MySQL)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DRIVER = Path(__file__).parent / "e2e_driver.py"
ENV = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": "utf-8"}
WIN = sys.platform == "win32"


def write_config(tmp_path: Path, **control: Any) -> Path:
    cfg = {
        "bot": {"TOKEN": "x", "SHUTDOWN_TIMEOUT": 20},
        "database": {
            "DB_HOST": "127.0.0.1",
            "DB_PORT": 1,
            "DB_USER": "u",
            "DB_PASSWORD": "p",
            "DB_NAME": "d",
            "DB_EAGER_CONNECT": False,
        },
        "control": {"PORT": 0, "STATE_FILE": "run/control.json", **control},
        "logging": {"DIR": "logs", "CONSOLE": False},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def cli_run(*args: str, cwd: Path = ROOT, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "euphoria", *args],
        cwd=cwd,
        env=ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def start_driver(tmp_path: Path, *extra: str, hang: bool = False) -> subprocess.Popen[str]:
    cfg = write_config(tmp_path)
    cmd = [sys.executable, str(DRIVER)]
    if hang:
        cmd.append("--hang-gateway")
    cmd += ["run", "--config", str(cfg), *extra]
    proc = subprocess.Popen(
        cmd,
        cwd=tmp_path,
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WIN else 0,
    )
    assert proc.stdout is not None
    deadline = time.time() + 20
    while time.time() < deadline:
        line = proc.stdout.readline()
        if "READY" in line:
            return proc
        if proc.poll() is not None:
            break
    proc.kill()
    out, err = proc.communicate()
    raise AssertionError(f"driver did not become ready: {out!r} {err!r}")


def ctl(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return cli_run("ctl", "--state-file", str(tmp_path / "run" / "control.json"), *args)


def test_run_missing_config_exit_1() -> None:
    r = cli_run("run", "--config", str(ROOT / "definitely_missing.json"))
    assert r.returncode == 1
    assert "definitely_missing.json" in r.stderr


def test_default_subcommand_missing_config_exit_1(tmp_path: Path) -> None:
    env = {**ENV, "EUPHORIA_CONFIG": str(tmp_path / "nope.json")}
    r = subprocess.run(
        [sys.executable, "-m", "euphoria"], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert r.returncode == 1 and "nope.json" in r.stderr


def test_legacy_main_py_shim_from_src(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["EUPHORIA_CONFIG"] = str(tmp_path / "nope.json")
    r = subprocess.run(
        [sys.executable, "main.py"], cwd=SRC, env=env, capture_output=True, text=True
    )
    assert r.returncode == 1 and "nope.json" in r.stderr


def test_ctl_nothing_listening_exit_1(tmp_path: Path) -> None:
    r = ctl(tmp_path, "--token", "t", "--port", "1", "ping")
    assert r.returncode == 1 and "cannot connect" in r.stderr


def test_ctl_no_state_no_token_exit_1(tmp_path: Path) -> None:
    r = ctl(tmp_path, "ping")
    assert r.returncode == 1 and "no control token" in r.stderr


def test_ctl_no_command_exit_2(tmp_path: Path) -> None:
    assert cli_run("ctl").returncode == 2


def test_full_lifecycle_ctl_exit(tmp_path: Path) -> None:
    proc = start_driver(tmp_path)
    try:
        state = json.loads((tmp_path / "run" / "control.json").read_text("utf-8"))
        assert (
            isinstance(state["pid"], int) and state["port"] > 0 and state["token"]
        )
        r = ctl(tmp_path, "ping")
        assert (r.returncode, r.stdout.strip()) == (0, "pong")
        r = ctl(tmp_path, "list")
        assert r.returncode == 0 and "0 extension(s) loaded" in r.stdout
        r = ctl(tmp_path, "status")
        assert r.returncode == 0 and "guilds: 0" in r.stdout and "shutting_down: False" in r.stdout
        r = ctl(tmp_path, "help")
        assert r.returncode == 0 and "load <extension>" in r.stdout
        r = ctl(tmp_path, "--json", "ping")
        assert json.loads(r.stdout)["message"] == "pong"
        r = ctl(tmp_path, "load", "../evil")
        assert r.returncode == 1 and "bad_arguments" in r.stderr
        r = ctl(tmp_path, "nosuchcmd")
        assert r.returncode == 1 and "unknown_command" in r.stderr
        r = ctl(tmp_path, "--token", "wrong", "ping")
        assert r.returncode == 1 and "unauthorized" in r.stderr
        r = ctl(tmp_path, "exit")
        assert (r.returncode, r.stdout.strip()) == (0, "Shutdown requested")
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not (tmp_path / "run" / "control.json").exists()
    log = (tmp_path / "logs" / "bot.log").read_text("utf-8")
    assert "Bot stopped (reason: control: exit)" in log
    assert (tmp_path / "logs" / "euphoria.log").exists()
    err = proc.stderr.read() if proc.stderr else ""
    assert "Traceback" not in err


def test_ctl_via_env_token(tmp_path: Path) -> None:
    proc = start_driver(tmp_path)
    try:
        st = json.loads((tmp_path / "run" / "control.json").read_text("utf-8"))
        env = {**ENV, "EUPHORIA_CONTROL_TOKEN": st["token"]}
        r = subprocess.run(
            [sys.executable, "-m", "euphoria", "ctl", "--port", str(st["port"]), "exit"],
            env=env,
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert r.returncode == 0, r.stderr
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.skipif(not WIN, reason="CTRL_BREAK_EVENT is Windows only")
def test_ctrl_break_graceful_exit_0(tmp_path: Path) -> None:
    proc = start_driver(tmp_path)
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not (tmp_path / "run" / "control.json").exists()
    assert "Bot stopped (reason: signal SIGBREAK)" in (tmp_path / "logs" / "bot.log").read_text(
        "utf-8"
    )


@pytest.mark.skipif(WIN, reason="POSIX signals")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_posix_signal_graceful_exit_0(tmp_path: Path, sig: int) -> None:
    proc = start_driver(tmp_path)
    try:
        proc.send_signal(sig)
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


@pytest.mark.skipif(not WIN, reason="CTRL_BREAK_EVENT is Windows only")
def test_second_signal_forces_exit_1(tmp_path: Path) -> None:
    proc = start_driver(tmp_path, hang=True)
    try:
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        time.sleep(1.0)
        assert proc.poll() is None
        proc.send_signal(signal.CTRL_BREAK_EVENT)
        assert proc.wait(timeout=15) == 1
    finally:
        if proc.poll() is None:
            proc.kill()
    assert (tmp_path / "logs" / "euphoria.log").exists()


def test_control_port_in_use_exit_1(tmp_path: Path) -> None:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        cfg = write_config(tmp_path, PORT=port)
        r = subprocess.run(
            [sys.executable, str(DRIVER), "run", "--config", str(cfg)],
            cwd=tmp_path,
            env=ENV,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        s.close()
    assert r.returncode == 1
    log = (tmp_path / "logs" / "euphoria.log").read_text("utf-8")
    assert "cannot bind" in log


def test_no_control_flag_no_state_file(tmp_path: Path) -> None:
    cfg = write_config(tmp_path)
    proc = subprocess.Popen(
        [sys.executable, str(DRIVER), "run", "--config", str(cfg), "--no-control"],
        cwd=tmp_path,
        env=ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WIN else 0,
    )
    assert proc.stdout is not None
    assert "READY" in proc.stdout.readline()
    assert not (tmp_path / "run" / "control.json").exists()
    proc.send_signal(signal.CTRL_BREAK_EVENT if WIN else signal.SIGTERM)
    assert proc.wait(timeout=20) == 0
