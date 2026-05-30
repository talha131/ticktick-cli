"""Settings loading. Pure I/O at the module boundary; pydantic validates.

Only TickTick-side settings live here. Anything AI-flavored (mode-window
schedules, ranking model choice, effort/snooze conventions) is the
workspace agent's concern — see docs/workspace/CLAUDE.md.template."""

from __future__ import annotations

import logging
from pathlib import Path
from pydantic import BaseModel, Field
from ruamel.yaml import YAML

log = logging.getLogger(__name__)
_yaml = YAML(typ="safe")


class SyncSettings(BaseModel):
    ttl_minutes: int = 5
    # Lookback window for POST /open/v1/task/completed. The endpoint
    # accepts an open-ended range, but pulling more than a month of
    # completions on every sync is wasteful for the `recent` use case;
    # widen this if you need deeper history.
    completions_lookback_days: int = 30


class FilterSettings(BaseModel):
    excluded_projects_by_name: list[str] = Field(default_factory=list)


class DatabaseSettings(BaseModel):
    # Path to the SQLite mirror. When None (default), the CLI resolves it
    # at use time to `<TICKTICK_CLI_HOME>/cache/tasks.db` — i.e. it tracks
    # whatever local-only config dir the user is on ($TICKTICK_CLI_HOME if
    # set, else $XDG_CONFIG_HOME/ticktick-cli, else ~/.config/ticktick-cli).
    # This keeps the default portable across macOS/Linux/Windows; if the
    # user has XDG_CONFIG_HOME pointing at %APPDATA% on Windows, the cache
    # follows automatically.
    #
    # Override to relocate the SQLite file explicitly (e.g. into the
    # git-synced workspace once you migrate off TickTick and SQLite
    # becomes the source of truth). `~` is expanded at use time.
    path: str | None = None


class Settings(BaseModel):
    sync: SyncSettings = Field(default_factory=SyncSettings)
    filters: FilterSettings = Field(default_factory=FilterSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)


def load_settings(path: Path) -> Settings:
    if not path.exists():
        return Settings()
    raw = _yaml.load(path.read_text()) or {}
    return Settings.model_validate(raw)
