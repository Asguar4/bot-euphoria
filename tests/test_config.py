"""AC 7, 8, 12, 27: config, settings, logging manager."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import utils.config as config_module
from euphoria.settings import CliOverrides, is_loopback, load_settings
from utils.config import Config, ConfigError, get_config_path, set_config_path
from utils.database import DatabaseSettings, _parse_bool
from utils.loggerManager import ConsoleSink, FileSink, Logger, LoggerManager

FIXTURES = Path(__file__).parent / "fixtures"

ROOT = Path(__file__).resolve().parents[1]
RAW_MIN: dict[str, Any] = json.loads((FIXTURES / "config_minimal.json").read_text("utf-8"))


def test_import_without_config_does_no_io(tmp_path: Path) -> None:
    """AC7: import with a CWD that has no config.json and a bogus EUPHORIA_CONFIG."""
    code = (
        "import utils.config, utils.database, utils.loggerManager\n"
        "from pathlib import Path\n"
        "assert utils.config._shared is None\n"
    )
    import os

    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "EUPHORIA_CONFIG": str(tmp_path / "nope")}
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True)
    assert r.returncode == 0, r.stderr


def test_cfg_lazy_load_and_same_object(tmp_path: Path) -> None:
    """AC7."""
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"a": 1}), encoding="utf-8")
    set_config_path(p)
    from utils.config import cfg

    assert cfg == {"a": 1}
    assert Config().config is cfg
    p.write_text(json.dumps({"a": 2, "b": "ъ"}), encoding="utf-8")
    Config().update_config()
    assert cfg == {"a": 2, "b": "ъ"}
    assert config_module.cfg is cfg  # type: ignore[attr-defined]


def test_update_config_failure_keeps_old(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_text('{"a": 1}', encoding="utf-8")
    set_config_path(p)
    c = Config()
    p.write_text("{bad", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid JSON"):
        c.update_config()
    assert c.config == {"a": 1}
    p.unlink()
    with pytest.raises(ConfigError):
        c.update_config()
    assert c.config == {"a": 1}


@pytest.mark.parametrize("content", ["[1]", "3", '"x"', "", "{"])
def test_config_non_object(tmp_path: Path, content: str) -> None:
    p = tmp_path / "c.json"
    p.write_text(content, encoding="utf-8")
    set_config_path(p)
    with pytest.raises(ConfigError, match=str(p).replace("\\", "\\\\")):
        Config()


def test_missing_config_names_path(tmp_path: Path) -> None:
    p = tmp_path / "missing.json"
    set_config_path(p)
    with pytest.raises(ConfigError) as ei:
        Config()
    assert str(p) in str(ei.value)


def test_bom_accepted(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    p.write_bytes(b"\xef\xbb\xbf" + '{"k": "привет"}'.encode())
    set_config_path(p)
    assert Config().config == {"k": "привет"}


def test_unknown_attribute() -> None:
    with pytest.raises(AttributeError):
        config_module.nothing  # type: ignore[attr-defined]  # noqa: B018


def test_config_path_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_config_path() == ROOT / "config.json"
    monkeypatch.setenv("EUPHORIA_CONFIG", str(tmp_path / "env.json"))
    assert get_config_path() == tmp_path / "env.json"
    set_config_path(tmp_path / "x.json")
    assert get_config_path() == tmp_path / "x.json"


def test_set_config_path_after_load_reloads(tmp_path: Path) -> None:
    """5.1: reset after load, next access reloads from the new path."""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text('{"v": "a"}', encoding="utf-8")
    b.write_text('{"v": "b"}', encoding="utf-8")
    set_config_path(a)
    assert Config().config["v"] == "a"
    set_config_path(b)
    assert Config().config["v"] == "b"
    from utils.config import cfg

    assert cfg["v"] == "b"


def test_set_config_path_after_load_leaves_prebound_cfg_empty(tmp_path: Path) -> None:
    """Evaluates developer deviation: names bound before set_config_path see an empty dict.

    Same dict object is retained (spec: 'always the same dict object'), but contents are cleared
    until the next ``Config()`` access. Documented as a note, not a failure.
    """
    a = tmp_path / "a.json"
    a.write_text('{"v": "a"}', encoding="utf-8")
    set_config_path(a)
    from utils.config import cfg

    bound = cfg
    set_config_path(a)
    assert bound is Config().config
    assert bound == {"v": "a"}


def test_prebound_cfg_empty_between_reset_and_access(tmp_path: Path) -> None:
    a = tmp_path / "a.json"
    a.write_text('{"v": "a"}', encoding="utf-8")
    set_config_path(a)
    from utils.config import cfg

    bound = cfg
    set_config_path(a)
    assert bound == {"v": "a"}


@pytest.mark.parametrize("value", [True, "true", "TRUE", "True"])
def test_parse_bool_true(value: object) -> None:
    assert _parse_bool(value, "K") is True


@pytest.mark.parametrize("value", [False, "false", "FALSE"])
def test_parse_bool_false(value: object) -> None:
    assert _parse_bool(value, "K") is False


@pytest.mark.parametrize("value", ["yes", "1", 1, 0, None, [], "", "tru"])
def test_parse_bool_bad(value: object) -> None:
    with pytest.raises(ConfigError, match="K"):
        _parse_bool(value, "K")


def _db(**over: Any) -> dict[str, Any]:
    base = dict(RAW_MIN["database"])
    base.update(over)
    return base


@pytest.mark.parametrize("key", ["DB_AUTOCOMMIT", "DB_ECHO", "DB_EAGER_CONNECT"])
@pytest.mark.parametrize("val", [True, False, "true", "false"])
def test_db_bool_keys_accept(key: str, val: object) -> None:
    DatabaseSettings.from_mapping(_db(**{key: val}))


def test_db_autocommit_yes_rejected() -> None:
    with pytest.raises(ConfigError, match="DB_AUTOCOMMIT"):
        DatabaseSettings.from_mapping(_db(DB_AUTOCOMMIT="yes"))


def test_db_missing_host_named() -> None:
    d = _db()
    del d["DB_HOST"]
    with pytest.raises(ConfigError, match="DB_HOST"):
        DatabaseSettings.from_mapping(d)


@pytest.mark.parametrize(
    ("key", "val"),
    [("DB_PORT", "3306"), ("DB_PORT", True), ("DB_HOST", 5), ("DB_USER", None), ("DB_NAME", [])],
)
def test_db_wrong_types(key: str, val: object) -> None:
    with pytest.raises(ConfigError, match=key):
        DatabaseSettings.from_mapping(_db(**{key: val}))


def test_db_minsize_gt_maxsize() -> None:
    with pytest.raises(ConfigError):
        DatabaseSettings.from_mapping(_db(DB_MINSIZE=5, DB_MAXSIZE=2))


def test_db_repr_hides_password() -> None:
    s = DatabaseSettings.from_mapping(_db(DB_PASSWORD="hunter2-секрет"))
    assert "hunter2" not in repr(s)
    assert s.password == "hunter2-секрет"
    assert s.eager_connect is True and s.close_timeout == 5.0 and s.autocommit is True


def test_db_close_timeout_and_lazy() -> None:
    s = DatabaseSettings.from_mapping(_db(DB_CLOSE_TIMEOUT=2, DB_EAGER_CONNECT=False))
    assert s.close_timeout == 2.0 and s.eager_connect is False


def test_load_settings_fixture_valid(tmp_path: Path) -> None:
    raw = json.loads((FIXTURES / "config_valid.json").read_text("utf-8"))
    s = load_settings(raw, base_dir=tmp_path)
    assert s.bot.token == "tok-абв"
    assert s.bot.extensions == ("modules.a",)
    assert s.bot.shutdown_timeout == 5.0
    assert s.control.port == 0
    assert s.control.host == "127.0.0.1"
    assert s.logging.level == "DEBUG"
    assert s.logging.console is False
    assert "tok-" not in repr(s.bot)
    assert s.control.state_file == ROOT / ".run" / "control.json"
    assert s.logging.dir == ROOT / "logs"


def test_load_settings_defaults(tmp_path: Path) -> None:
    s = load_settings(RAW_MIN, base_dir=tmp_path)
    assert s.control.port == 48600
    assert s.control.enabled is True
    assert s.control.max_connections == 8
    assert s.control.idle_timeout == 60.0
    assert s.control.command_timeout == 120.0
    assert s.bot.shutdown_timeout == 30.0
    assert s.bot.timezone == "Europe/Moscow"


def test_load_settings_relative_paths(tmp_path: Path) -> None:
    raw = {
        **RAW_MIN,
        "control": {"STATE_FILE": "st/c.json"},
        "logging": {"DIR": "lg"},
    }
    s = load_settings(raw, base_dir=tmp_path)
    assert s.control.state_file == tmp_path / "st" / "c.json"
    assert s.logging.dir == tmp_path / "lg"


def test_load_settings_bad_types_fixture(tmp_path: Path) -> None:
    raw = json.loads((FIXTURES / "config_bad_types.json").read_text("utf-8"))
    with pytest.raises(ConfigError, match=r"bot\.TOKEN"):
        load_settings(raw, base_dir=tmp_path)


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"bot": {}}, "bot.TOKEN"),
        ({"bot": {"TOKEN": ""}}, "bot.TOKEN"),
        ({"bot": {"TOKEN": "t", "EXTENSIONS": "x"}}, "EXTENSIONS"),
        ({"bot": {"TOKEN": "t", "EXTENSIONS": [1]}}, "EXTENSIONS"),
        ({"bot": {"TOKEN": "t", "SHUTDOWN_TIMEOUT": 0}}, "SHUTDOWN_TIMEOUT"),
        ({"bot": {"TOKEN": "t", "SHUTDOWN_TIMEOUT": "5"}}, "SHUTDOWN_TIMEOUT"),
        ({"control": {"PORT": 70000}}, "control.PORT"),
        ({"control": {"PORT": -1}}, "control.PORT"),
        ({"control": {"PORT": "1"}}, "control.PORT"),
        ({"control": {"PORT": True}}, "control.PORT"),
        ({"control": {"ENABLED": "yes"}}, "ENABLED"),
        ({"control": {"HOST": "0.0.0.0"}}, "control.HOST"),
        ({"control": {"HOST": "192.168.1.5", "ALLOW_REMOTE": True}}, "control.TOKEN"),
        ({"control": {"HOST": "0.0.0.0", "ALLOW_REMOTE": True, "TOKEN": "short"}}, "TOKEN"),
        ({"control": {"ALLOW_REMOTE": "true"}}, "ALLOW_REMOTE"),
        ({"logging": {"LEVEL": "TRACE"}}, "logging.LEVEL"),
        ({"logging": {"CONSOLE": "no"}}, "CONSOLE"),
        ({"control": []}, "control"),
    ],
)
def test_load_settings_errors(patch: dict[str, Any], match: str, tmp_path: Path) -> None:
    raw = {**RAW_MIN, **patch}
    with pytest.raises(ConfigError, match=match):
        load_settings(raw, base_dir=tmp_path)


def test_load_settings_missing_sections(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="bot"):
        load_settings({"database": RAW_MIN["database"]}, base_dir=tmp_path)
    with pytest.raises(ConfigError, match="database"):
        load_settings({"bot": {"TOKEN": "t"}}, base_dir=tmp_path)


def test_remote_allowed_with_token(tmp_path: Path) -> None:
    raw = {
        **RAW_MIN,
        "control": {"HOST": "0.0.0.0", "ALLOW_REMOTE": True, "TOKEN": "x" * 16},
    }
    s = load_settings(raw, base_dir=tmp_path)
    assert s.control.host == "0.0.0.0" and s.control.token == "x" * 16


def test_cli_overrides(tmp_path: Path) -> None:
    ov = CliOverrides(
        log_dir=tmp_path / "L",
        control_host="127.0.0.2",
        control_port=5,
        control_enabled=False,
        console_log=False,
    )
    s = load_settings(RAW_MIN, base_dir=tmp_path, overrides=ov)
    assert s.logging.dir == tmp_path / "L"
    assert (s.control.host, s.control.port, s.control.enabled) == ("127.0.0.2", 5, False)
    assert s.logging.console is False


def test_cli_override_nonloopback_host_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(RAW_MIN, base_dir=tmp_path, overrides=CliOverrides(control_host="0.0.0.0"))


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.5.5.5", True),
        ("::1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("0.0.0.0", False),
        ("192.168.1.5", False),
        ("example.com", False),
        ("", False),
    ],
)
def test_is_loopback(host: str, expected: bool) -> None:
    assert is_loopback(host) is expected


def test_import_logger_manager_creates_no_dirs(tmp_path: Path) -> None:
    import os

    code = "import utils.loggerManager, os; print(os.getcwd())"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True)
    assert r.returncode == 0, r.stderr
    assert list(tmp_path.iterdir()) == []


def test_logger_writes_to_configured_dir_and_shutdown_releases(tmp_path: Path) -> None:
    d = tmp_path / "newlogs"
    lm = LoggerManager()
    lm.configure(d)
    assert lm.log_dir == d
    log = lm.create_logger("моя")
    log.info("привет мир")
    log.success("ok")
    log.exception("no active exc")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("caught")
    f = d / "моя.log"
    text = f.read_text("utf-8")
    assert "привет мир" in text and "[SUCCESS]" in text and "ValueError: boom" in text
    lm.shutdown()
    assert lm.loggers == {}
    f.unlink()
    lm.shutdown()
    shutil.rmtree(d)


def test_logger_manager_singleton_and_get() -> None:
    assert LoggerManager() is LoggerManager()
    a = LoggerManager().create_logger("x")
    assert LoggerManager().get_logger("x") is a
    assert LoggerManager().create_logger("x") is a
    LoggerManager().remove_logger("x")
    assert "x" not in LoggerManager().loggers
    LoggerManager().remove_logger("never")


def test_configure_affects_only_later_loggers(tmp_path: Path) -> None:
    lm = LoggerManager()
    first = lm.create_logger("first")
    lm.configure(tmp_path / "other")
    second = lm.create_logger("second")
    assert isinstance(first.sinks[0], FileSink)
    assert Path(first.sinks[0].file_path).parent != Path(second.sinks[0].file_path).parent


def test_console_flag_adds_console_sink(tmp_path: Path) -> None:
    lm = LoggerManager()
    lm.configure(tmp_path, console=True)
    log = lm.create_logger("c")
    assert any(isinstance(s, ConsoleSink) for s in log.sinks)


def test_logger_sink_failure_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    class Bad(FileSink):
        def __init__(self) -> None:
            pass

        def write(self, level: str, message: str) -> None:
            raise OSError("disk")

    log = Logger()
    log.sinks.append(Bad())
    with caplog.at_level("WARNING"):
        log.info("a")
        log.info("b")
        log.error("c")
    assert sum("log sink" in r.message for r in caplog.records) == 1


def test_logger_non_str_message() -> None:
    log = Logger()
    got: list[str] = []

    class S(FileSink):
        def __init__(self) -> None:
            pass

        def write(self, level: str, message: str) -> None:
            got.append(message)

    log.sinks.append(S())
    log.info(123)  # type: ignore[arg-type]
    assert got == ["123"]
