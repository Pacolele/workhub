"""Load ~/.config/workhub/config.toml (or $WORKHUB_CONFIG)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or default).expanduser() / "workhub"


CONFIG_PATH = Path(os.environ.get("WORKHUB_CONFIG") or "~/.config/workhub/config.toml").expanduser()
DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share")
STATE_DIR = _xdg("XDG_STATE_HOME", "~/.local/state")
WINDOW_MODES = ("app", "browser", "none")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class GitLabConfig:
    host: str
    groups: tuple[str, ...]


@dataclass(frozen=True)
class JiraConfig:
    site: str
    email: str
    jql: str
    role_fields: tuple[str, ...]
    token_file: Path
    api_base: str


@dataclass(frozen=True)
class BoardConfig:
    port: int
    window: str
    sync_minutes: int


@dataclass(frozen=True)
class Config:
    path: Path
    gitlab: GitLabConfig | None
    jira: JiraConfig | None
    board: BoardConfig

    @property
    def db_path(self) -> Path:
        return DATA_DIR / "hub.db"

    @property
    def log_path(self) -> Path:
        return STATE_DIR / "server.log"


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.is_file():
        raise ConfigError(f"No config at {path}. Copy config.example.toml from the workhub folder there.")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    gitlab = None
    if "gitlab" in raw:
        t = _table(raw, "gitlab")
        groups = t.get("groups", [t["group"]] if "group" in t else [])
        gitlab = GitLabConfig(host=_text(t, "gitlab", "host"), groups=_texts(groups, "gitlab", "groups"))
        if not gitlab.groups:
            raise ConfigError(f"{path}: [gitlab] groups is empty")

    jira = None
    if "jira" in raw:
        t = _table(raw, "jira")
        site = _text(t, "jira", "site").rstrip("/")
        if not site.startswith("https://"):
            raise ConfigError(f"{path}: [jira] site must start with https://")
        api_base = str(t.get("api_base", "")).strip().rstrip("/")
        if api_base and not api_base.startswith("https://"):
            raise ConfigError(f"{path}: [jira] api_base must start with https://")
        jira = JiraConfig(
            site=site,
            email=_text(t, "jira", "email"),
            jql=" ".join(_text(t, "jira", "jql").split()),
            role_fields=_texts(t.get("role_fields", []), "jira", "role_fields"),
            token_file=Path(str(t.get("token_file", "~/.config/workhub/jira-token"))).expanduser(),
            api_base=api_base,
        )

    t = _table(raw, "board") if "board" in raw else {}
    board = BoardConfig(
        port=_int(t, "port", 8765),
        window=str(t.get("window", "app")),
        sync_minutes=_int(t, "sync_minutes", 10),
    )
    if board.window not in WINDOW_MODES:
        raise ConfigError(f"{path}: [board] window must be one of {', '.join(WINDOW_MODES)}")
    if not 1024 <= board.port <= 65535:
        raise ConfigError(f"{path}: [board] port must be between 1024 and 65535")
    return Config(path=path, gitlab=gitlab, jira=jira, board=board)


def _table(raw: dict, name: str) -> dict:
    value = raw[name]
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _text(t: dict, section: str, key: str) -> str:
    value = t.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{section}] {key} must be a non-empty string")
    return value.strip()


def _texts(value: object, section: str, key: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"[{section}] {key} must be a list of strings")
    return tuple(v.strip() for v in value)


def _int(t: dict, key: str, default: int) -> int:
    value = t.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"[board] {key} must be a whole number")
    return value
