"""workhub: your Jira tickets and GitLab issues/MRs in one place. Plan the day, keep a priority list, see what's next."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import jira, sync
from .errors import SourceError
from .opener import open_url, open_window
from .store import LANE_KEYS, LANES, Store, split_title_url

SOURCE_NAMES = {"jira": "Jira", "gitlab": "GitLab", "local": "Daily"}
# Talks to our own server on 127.0.0.1, so never through a proxy.
_LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workhub", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")
    sub.add_parser("today", help="today's plan and what's next (the default)")
    p = sub.add_parser("next", help="the item to work on next")
    p.add_argument("--short", action="store_true", help="one short line, for a tmux status bar")
    p = sub.add_parser("list", help="every lane, or one lane")
    p.add_argument("lane", nargs="?", choices=LANE_KEYS)
    sub.add_parser("sync", help="pull from Jira and GitLab now")
    sub.add_parser("board", help="open the drag-and-drop board (starts the board server if needed)")
    sub.add_parser("stop", help="stop the board server")
    sub.add_parser("serve", help="run the board server in the foreground")
    p = sub.add_parser("add", help="add a daily task, to Today unless --lane says otherwise (a link is attached)")
    p.add_argument("text")
    p.add_argument("--lane", default="today", choices=LANE_KEYS)
    p = sub.add_parser("open", help="open an item in the browser (default: the next item)")
    p.add_argument("ref", nargs="?", help="PROJ-123, project#253, project!278, ...")
    sub.add_parser("jira-token", help="save your Jira API token and test it")
    args = parser.parse_args(argv)

    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as e:
        print(f"workhub: {e}", file=sys.stderr)
        return 2
    commands = {
        "today": cmd_today,
        "next": cmd_next,
        "list": cmd_list,
        "sync": cmd_sync,
        "board": cmd_board,
        "stop": cmd_stop,
        "serve": cmd_serve,
        "add": cmd_add,
        "open": cmd_open,
        "jira-token": cmd_jira_token,
    }
    try:
        return commands[args.cmd or "today"](cfg, args) or 0
    except KeyboardInterrupt:
        return 130


# --- commands ------------------------------------------------------------------


def cmd_today(cfg: config_mod.Config, args) -> int:
    store = Store(cfg.db_path)
    items = store.items()
    width = _width()
    _print_lane("Today", [i for i in items if i["lane"] == "today"], width)
    nxt = store.next_item()
    print()
    if nxt:
        print(_clip(f"Next: {_label(nxt)}   (workhub open)", width))
    else:
        print("Next: nothing planned. Drag something into Today or Up next (workhub board).")
    inbox = sum(1 for i in items if i["lane"] == "inbox")
    if inbox:
        print(f"Inbox: {inbox} item{'s' if inbox != 1 else ''} to sort (workhub board).")
    _print_sources(store)
    return 0


def cmd_next(cfg: config_mod.Config, args) -> int:
    item = Store(cfg.db_path).next_item()
    if args.short:
        print(_clip(f"▶ {_label(item)}", 50) if item else "")
        return 0
    if not item:
        print("Nothing planned. Drag something into Today or Up next (workhub board).")
        return 0
    print(_clip(_line(item), _width()))
    if item["url"]:
        print(item["url"])
    _print_todos(item, _width(), indent=0)
    return 0


def cmd_list(cfg: config_mod.Config, args) -> int:
    store = Store(cfg.db_path)
    items = store.items()
    width = _width()
    for key, name in LANES:
        if args.lane and key != args.lane:
            continue
        _print_lane(name, [i for i in items if i["lane"] == key], width)
        print()
    _print_sources(store)
    return 0


def cmd_sync(cfg: config_mod.Config, args) -> int:
    if _server_up(cfg):  # let the board do it, so two syncs never run side by side
        report = _local(cfg, "/api/sync", {}, timeout=600)["report"]
    else:
        report = sync.run(cfg, Store(cfg.db_path))
    failed = False
    for name, result in report.items():
        label = SOURCE_NAMES.get(name, name)
        if "error" in result:
            failed = True
            print(f"{label}: {result['error']}")
            continue
        parts = [f"{result['count']} open"]
        for key, word in (("new", "new"), ("finished", "finished"), ("returned", "back")):
            if result[key]:
                parts.append(f"{len(result[key])} {word} ({', '.join(result[key][:8])})")
        print(f"{label}: " + ", ".join(parts))
    return 1 if failed else 0


def cmd_board(cfg: config_mod.Config, args) -> int:
    if not _server_up(cfg):
        _start_server(cfg)
    url = f"http://localhost:{cfg.board.port}/"
    syncing = f"It syncs every {cfg.board.sync_minutes} min; `workhub stop` stops it."
    if cfg.board.window == "none":
        print(f"Board server running at {url}. Open or reload your pinned tab. {syncing}")
        return 0
    used = open_window(url, cfg.board.window)
    print(f"Board: {url} (opened in {used}). {syncing}")
    return 0


def cmd_stop(cfg: config_mod.Config, args) -> int:
    if not _server_up(cfg):
        print("The board server is not running.")
        return 0
    _local(cfg, "/api/shutdown", {})
    print("Board server stopped.")
    return 0


def cmd_serve(cfg: config_mod.Config, args) -> int:
    from .server import serve

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    serve(cfg)
    return 0


def cmd_add(cfg: config_mod.Config, args) -> int:
    title, url = split_title_url(args.text)
    try:
        Store(cfg.db_path).add_local(title, url, args.lane)
    except ValueError as e:
        print(f"workhub: {e}", file=sys.stderr)
        return 2
    lane = dict(LANES)[args.lane]
    print(f"Added to {lane}: {title}" + (f" ({url})" if url else ""))
    return 0


def cmd_open(cfg: config_mod.Config, args) -> int:
    store = Store(cfg.db_path)
    item = store.find(args.ref) if args.ref else store.next_item()
    if not item:
        print(f"Nothing on the board matches {args.ref!r}." if args.ref else "Nothing planned to open.")
        return 1
    if not item["url"]:
        print(f"{_label(item)} has no link.")
        return 1
    open_url(item["url"])
    print(f"Opened {_label(item)}")
    return 0


def cmd_jira_token(cfg: config_mod.Config, args) -> int:
    if not cfg.jira:
        print(f"workhub: add a [jira] section to {cfg.path} first (see config.example.toml)", file=sys.stderr)
        return 2
    print("Create a token at https://id.atlassian.com/manage-profile/security/api-tokens")
    print("A scoped token needs the Jira scopes read:jira-work and read:jira-user.")
    token = getpass.getpass(f"API token for {cfg.jira.email} (input hidden): ").strip()
    if not token:
        print("No token entered; nothing changed.")
        return 1
    path = cfg.jira.token_file
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)  # also tightens a file that already existed
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(token + "\n")
    if os.environ.get("JIRA_API_TOKEN"):
        print("Note: JIRA_API_TOKEN is set in your environment and wins over the file.")
    try:
        me = jira.check(cfg.jira, Store(cfg.db_path))
    except SourceError as e:
        print(f"Saved to {path}, but the test failed: {e}")
        return 1
    print(f"Saved to {path}. Jira knows you as {me.get('displayName', me['accountId'])}. Run `workhub sync`.")
    return 0


# --- board server process --------------------------------------------------------


def _server_up(cfg: config_mod.Config) -> bool:
    try:
        return _local(cfg, "/api/ping", timeout=1).get("app") == "workhub"
    except (OSError, ValueError):
        return False


def _local(cfg: config_mod.Config, path: str, body: dict | None = None, timeout: float = 10) -> dict:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{cfg.board.port}{path}", data=data)
    if data is not None:
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Workhub", "1")
    with _LOCAL.open(request, timeout=timeout) as response:
        return json.load(response)


def _start_server(cfg: config_mod.Config) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.log_path, "ab") as log:
        subprocess.Popen(
            [sys.executable, "-m", "workhub", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            cwd=Path.home(),
            start_new_session=True,
        )
    for _ in range(100):
        time.sleep(0.1)
        if _server_up(cfg):
            return
    raise SystemExit(f"workhub: the board server did not start. See {cfg.log_path}")


# --- printing ---------------------------------------------------------------------


def _width() -> int:
    return max(60, shutil.get_terminal_size((110, 24)).columns)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(width - 1, 0)] + "…"


def _label(item: dict) -> str:
    return f"{item['ref']} {item['title']}" if item["ref"] else item["title"]


def _line(item: dict) -> str:
    roles = f"  [{', '.join(item['roles'])}]" if item["roles"] else ""
    return f"{item['ref'] or '-':<14.14} {item['kind']:<8.8} {item['status']:<19.19} {item['title']}{roles}"


def _print_lane(name: str, items: list[dict], width: int) -> None:
    print(f"{name} ({len(items)})")
    if not items:
        print("   (empty)")
    for n, item in enumerate(items, 1):
        print(_clip(f"{n:>3}. {_line(item)}", width))
        _print_todos(item, width, indent=5)


def _print_todos(item: dict, width: int, indent: int) -> None:
    for todo in item.get("todos", []):
        print(_clip(f"{' ' * indent}☐ {todo['text']}", width))


def _print_sources(store: Store) -> None:
    for source in store.sources():
        name = SOURCE_NAMES.get(source["name"], source["name"])
        when = _ago(source["last_success"]) if source["last_success"] else "never"
        line = f"{name}: synced {when}"
        if source["error"]:
            line += f". Last sync failed: {source['error']}"
        print(line)


def _ago(iso: str) -> str:
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"
