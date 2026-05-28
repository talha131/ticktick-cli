"""Tests for the secrets.env loader and home-dir resolution in cli."""

from __future__ import annotations

import os
from pathlib import Path
import pytest

from ticktick_cli import cli


# ---- _home() resolution -----------------------------------------------------


def test_home_uses_explicit_override(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TICKTICK_CLI_HOME", str(tmp_path / "custom"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "should-not-win"))
    assert cli._home() == tmp_path / "custom"


def test_home_uses_xdg_config_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TICKTICK_CLI_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert cli._home() == tmp_path / "xdg" / "ticktick-cli"


def test_home_defaults_to_dot_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TICKTICK_CLI_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli._home() == tmp_path / ".config" / "ticktick-cli"


# ---- _resolve_db_path() portability ----------------------------------------


def test_db_path_defaults_to_home_cache(monkeypatch, tmp_path: Path) -> None:
    """When `database.path` is unset in settings, the resolved DB path
    must follow `_home()` — so on Windows with XDG_CONFIG_HOME=%APPDATA%
    (or anywhere else), the cache co-locates with the rest of local-only
    state instead of pointing at a hardcoded ~/.config/... path."""
    from ticktick_cli.config import Settings
    monkeypatch.setenv("TICKTICK_CLI_HOME", str(tmp_path / "custom-home"))
    settings = Settings()  # all defaults; database.path is None
    assert cli._resolve_db_path(settings) == tmp_path / "custom-home" / "cache" / "tasks.db"


def test_db_path_respects_explicit_override(monkeypatch, tmp_path: Path) -> None:
    from ticktick_cli.config import Settings, DatabaseSettings
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = Settings(database=DatabaseSettings(path="~/elsewhere/tasks.db"))
    assert cli._resolve_db_path(settings) == tmp_path / "elsewhere" / "tasks.db"


# ---- _load_secrets_file ----------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch, tmp_path: Path):
    """Each test gets its own TICKTICK_CLI_HOME and a clean env slate
    for the keys we touch."""
    monkeypatch.setenv("TICKTICK_CLI_HOME", str(tmp_path))
    monkeypatch.delenv("TICKTICK_CLIENT_ID", raising=False)
    monkeypatch.delenv("TICKTICK_CLIENT_SECRET", raising=False)


def test_loader_no_file_is_noop(tmp_path: Path) -> None:
    cli._load_secrets_file()
    assert "TICKTICK_CLIENT_ID" not in os.environ


def test_loader_parses_simple_kv(tmp_path: Path) -> None:
    (tmp_path / "secrets.env").write_text(
        "TICKTICK_CLIENT_ID=abc\nTICKTICK_CLIENT_SECRET=xyz\n"
    )
    cli._load_secrets_file()
    assert os.environ["TICKTICK_CLIENT_ID"] == "abc"
    assert os.environ["TICKTICK_CLIENT_SECRET"] == "xyz"


def test_loader_handles_quotes_and_comments(tmp_path: Path) -> None:
    (tmp_path / "secrets.env").write_text(
        "# this is a comment\n"
        '\n'
        'TICKTICK_CLIENT_ID="quoted-id"\n'
        "TICKTICK_CLIENT_SECRET='single-quoted'\n"
        "  # indented comment\n"
    )
    cli._load_secrets_file()
    assert os.environ["TICKTICK_CLIENT_ID"] == "quoted-id"
    assert os.environ["TICKTICK_CLIENT_SECRET"] == "single-quoted"


def test_loader_does_not_override_existing_env(monkeypatch, tmp_path: Path) -> None:
    """Shell env should win over the file — lets the user override one-off."""
    monkeypatch.setenv("TICKTICK_CLIENT_ID", "from-shell")
    (tmp_path / "secrets.env").write_text("TICKTICK_CLIENT_ID=from-file\n")
    cli._load_secrets_file()
    assert os.environ["TICKTICK_CLIENT_ID"] == "from-shell"


def test_loader_skips_malformed_lines(tmp_path: Path) -> None:
    (tmp_path / "secrets.env").write_text(
        "no-equals-sign\n"
        "TICKTICK_CLIENT_ID=ok\n"
        "=value-with-empty-key\n"
    )
    cli._load_secrets_file()
    assert os.environ["TICKTICK_CLIENT_ID"] == "ok"
