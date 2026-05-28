import pytest
from pathlib import Path
from ticktick_cli.config import Settings, load_settings


def test_settings_defaults_when_file_missing(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "settings.yml")
    assert s.sync.ttl_minutes == 5
    assert s.filters.excluded_projects_by_name == []
    # database.path defaults to None — the CLI resolves it at use time
    # so the cache tracks whichever local-only config dir is in effect
    # (XDG-aware on Linux/macOS, %APPDATA%-friendly on Windows).
    assert s.database.path is None


def test_settings_parsed_from_yaml(tmp_path: Path) -> None:
    p = tmp_path / "settings.yml"
    p.write_text(
        "sync:\n  ttl_minutes: 10\n"
        "filters:\n  excluded_projects_by_name: [Someday, Archive]\n"
    )
    s = load_settings(p)
    assert s.sync.ttl_minutes == 10
    assert s.filters.excluded_projects_by_name == ["Someday", "Archive"]


def test_settings_database_path_overridable(tmp_path: Path) -> None:
    p = tmp_path / "settings.yml"
    p.write_text(
        "database:\n  path: ~/Documents/Tasks/cache/tasks.db\n"
    )
    s = load_settings(p)
    assert s.database.path == "~/Documents/Tasks/cache/tasks.db"


def test_settings_malformed_yaml_raises(tmp_path: Path) -> None:
    p = tmp_path / "settings.yml"
    p.write_text("sync:\n  ttl_minutes: not-an-int\n")
    with pytest.raises(ValueError):
        load_settings(p)
