"""SQLite store: items synced from Jira and GitLab, plus your own lanes, order and notes.

A sync only writes the remote fields (title, status, ...). Lane, position and note
are yours and survive every sync.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

LANES = (
    ("inbox", "Inbox"),
    ("today", "Today"),
    ("next", "Up next"),
    ("waiting", "Waiting"),
    ("later", "Later"),
)
LANE_KEYS = tuple(key for key, _ in LANES)
# "What's next" is the top of the first non-empty lane here.
NEXT_LANES = ("today", "next")
# An item that disappears and comes back within this window (an API hiccup, a quick
# reopen) goes back to its old lane. Later than that it lands in Inbox again.
RESTORE_WINDOW = timedelta(hours=24)
REMOTE_FIELDS = ("kind", "ref", "title", "url", "status", "status_key", "roles", "info", "remote_updated")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    kind           TEXT NOT NULL DEFAULT '',
    ref            TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL,
    url            TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT '',
    status_key     TEXT NOT NULL DEFAULT '',
    roles          TEXT NOT NULL DEFAULT '[]',
    info           TEXT NOT NULL DEFAULT '{}',
    remote_updated TEXT,
    lane           TEXT NOT NULL DEFAULT 'inbox',
    position       INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    active         INTEGER NOT NULL DEFAULT 1,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS items_board ON items (active, lane, position);
CREATE TABLE IF NOT EXISTS sources (
    name         TEXT PRIMARY KEY,
    last_attempt TEXT,
    last_success TEXT,
    error        TEXT,
    item_count   INTEGER
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todos (
    id         TEXT PRIMARY KEY,
    item_id    TEXT NOT NULL,
    text       TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS todos_item ON todos (item_id, position);
"""
MAX_TODO = 300


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_http(url: str) -> bool:
    return bool(re.match(r"https?://", url or "", re.IGNORECASE))


def split_title_url(text: str) -> tuple[str, str]:
    """Pull the first http(s) link out of a task text: 'Read spec https://x' -> ('Read spec', 'https://x')."""
    match = _URL.search(text)
    if not match:
        return text.strip(), ""
    title = (text[: match.start()] + text[match.end() :]).strip() or match.group(0)
    return " ".join(title.split()), match.group(0)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 15000")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            else:
                self._db.execute("COMMIT")

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # --- reading ---------------------------------------------------------------

    def items(self) -> list[dict]:
        """Every item on the board, lane by lane, in your order, each with its open to-dos."""
        lane_order = " ".join(f"WHEN '{key}' THEN {i}" for i, key in enumerate(LANE_KEYS))
        rows = self._rows(
            f"SELECT * FROM items WHERE active = 1 ORDER BY CASE lane {lane_order} ELSE 99 END, position, first_seen"
        )
        return self._with_todos([_decode(r) for r in rows])

    def next_item(self) -> dict | None:
        for lane in NEXT_LANES:
            rows = self._rows(
                "SELECT * FROM items WHERE active = 1 AND lane = ? ORDER BY position, first_seen LIMIT 1", (lane,)
            )
            if rows:
                return self._with_todos([_decode(rows[0])])[0]
        return None

    def get(self, item_id: str) -> dict | None:
        rows = self._rows("SELECT * FROM items WHERE id = ? AND active = 1", (item_id,))
        return self._with_todos([_decode(rows[0])])[0] if rows else None

    def _with_todos(self, items: list[dict]) -> list[dict]:
        if not items:
            return items
        wanted = {item["id"] for item in items}
        per_item: dict[str, list[dict]] = {}
        rows = self._rows(
            "SELECT t.id, t.item_id, t.text FROM todos t JOIN items i ON i.id = t.item_id"
            " WHERE i.active = 1 ORDER BY t.position, t.created_at"
        )
        for row in rows:
            if row["item_id"] in wanted:
                per_item.setdefault(row["item_id"], []).append({"id": row["id"], "text": row["text"]})
        for item in items:
            item["todos"] = per_item.get(item["id"], [])
        return items

    def find(self, key: str) -> dict | None:
        """Look an item up by id or by reference (PROJ-123, project#253, project!278), case-insensitively."""
        rows = self._rows(
            "SELECT * FROM items WHERE active = 1 AND (id = ? OR ref = ? COLLATE NOCASE) ORDER BY first_seen LIMIT 1",
            (key, key),
        )
        return self._with_todos([_decode(rows[0])])[0] if rows else None

    def sources(self) -> list[dict]:
        return [dict(r) for r in self._rows("SELECT * FROM sources ORDER BY name")]

    # --- sync ------------------------------------------------------------------

    def apply_sync(self, source: str, fetched: list[dict], now: str) -> dict[str, list[str]]:
        """Merge one source's complete result. Only call this when the fetch fully succeeded:
        anything of this source that is missing from `fetched` counts as finished."""
        changes: dict[str, list[str]] = {"new": [], "finished": [], "returned": []}
        now_dt = datetime.fromisoformat(now)
        assignments = ", ".join(f"{field} = ?" for field in REMOTE_FIELDS)
        with self._tx() as db:
            rows = db.execute("SELECT id, ref, active, finished_at FROM items WHERE source = ?", (source,))
            known = {r["id"]: r for r in rows}
            seen: set[str] = set()
            for item in fetched:
                item_id = item["id"]
                if item_id in seen:
                    continue
                seen.add(item_id)
                values = _remote_values(item)
                label = item.get("ref") or item["title"]
                row = known.get(item_id)
                if row is None:
                    db.execute(
                        "INSERT INTO items (id, source, kind, ref, title, url, status, status_key, roles, info,"
                        " remote_updated, lane, position, first_seen, last_seen)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'inbox', ?, ?, ?)",
                        (item_id, source, *values, _end_of(db, "inbox"), now, now),
                    )
                    changes["new"].append(label)
                    continue
                db.execute(f"UPDATE items SET {assignments}, last_seen = ? WHERE id = ?", (*values, now, item_id))
                if not row["active"]:
                    finished = row["finished_at"]
                    recent = bool(finished) and now_dt - datetime.fromisoformat(finished) < RESTORE_WINDOW
                    if recent:
                        db.execute("UPDATE items SET active = 1, finished_at = NULL WHERE id = ?", (item_id,))
                    else:
                        db.execute(
                            "UPDATE items SET active = 1, finished_at = NULL, lane = 'inbox', position = ?"
                            " WHERE id = ?",
                            (_end_of(db, "inbox"), item_id),
                        )
                    changes["returned"].append(label)
            for item_id, row in known.items():
                if row["active"] and item_id not in seen:
                    db.execute("UPDATE items SET active = 0, finished_at = ? WHERE id = ?", (now, item_id))
                    changes["finished"].append(row["ref"] or item_id)
        return changes

    def record_source(self, name: str, when: str, error: str | None = None, count: int | None = None) -> None:
        with self._tx() as db:
            if error is None:
                db.execute(
                    "INSERT INTO sources (name, last_attempt, last_success, error, item_count)"
                    " VALUES (?, ?, ?, NULL, ?)"
                    " ON CONFLICT(name) DO UPDATE SET last_attempt = excluded.last_attempt,"
                    " last_success = excluded.last_success, error = NULL, item_count = excluded.item_count",
                    (name, when, when, count),
                )
            else:
                db.execute(
                    "INSERT INTO sources (name, last_attempt, error) VALUES (?, ?, ?)"
                    " ON CONFLICT(name) DO UPDATE SET last_attempt = excluded.last_attempt, error = excluded.error",
                    (name, when, error),
                )

    # --- your planning ---------------------------------------------------------

    def move(self, item_id: str, lane: str, order: list[str]) -> None:
        """Put an item in `lane`. `order` is the lane's ids top to bottom as the board shows them after the drop;
        ids that are not in the lane are ignored and lane items missing from it keep their order below."""
        if lane not in LANE_KEYS:
            raise ValueError(f"Unknown lane {lane!r}")
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone():
                raise KeyError(item_id)
            db.execute("UPDATE items SET lane = ? WHERE id = ?", (lane, item_id))
            others = [
                r["id"]
                for r in db.execute(
                    "SELECT id FROM items WHERE active = 1 AND lane = ? AND id != ? ORDER BY position, first_seen",
                    (lane, item_id),
                )
            ]
            in_lane = set(others) | {item_id}
            wanted = [i for i in dict.fromkeys(order) if i in in_lane]
            if item_id not in wanted:
                wanted.insert(0, item_id)
            placed = set(wanted)
            final = wanted + [i for i in others if i not in placed]
            db.executemany("UPDATE items SET position = ? WHERE id = ?", [(pos, i) for pos, i in enumerate(final)])

    def set_note(self, item_id: str, note: str) -> None:
        with self._tx() as db:
            if db.execute("UPDATE items SET note = ? WHERE id = ? AND active = 1", (note, item_id)).rowcount == 0:
                raise KeyError(item_id)

    def add_local(self, title: str, url: str = "", lane: str = "inbox") -> str:
        """Add a task of your own (not from Jira or GitLab)."""
        if lane not in LANE_KEYS:
            raise ValueError(f"Unknown lane {lane!r}")
        if not title.strip():
            raise ValueError("A task needs a title")
        if url and not is_http(url):
            raise ValueError("Links must start with http:// or https://")
        now = now_iso()
        item_id = f"local:{uuid.uuid4().hex[:12]}"
        with self._tx() as db:
            db.execute(
                "INSERT INTO items (id, source, kind, title, url, lane, position, first_seen, last_seen)"
                " VALUES (?, 'local', 'Task', ?, ?, ?, ?, ?, ?)",
                (item_id, title.strip(), url, lane, _end_of(db, lane), now, now),
            )
        return item_id

    def delete_local(self, item_id: str) -> None:
        """Delete one of your own daily tasks, with its note and to-dos.
        Jira and GitLab items can't be deleted here; they leave the board when they close there."""
        with self._tx() as db:
            if db.execute("DELETE FROM items WHERE id = ? AND source = 'local'", (item_id,)).rowcount == 0:
                raise KeyError(item_id)
            db.execute("DELETE FROM todos WHERE item_id = ?", (item_id,))

    # --- to-dos on a card ------------------------------------------------------

    def add_todo(self, item_id: str, text: str) -> str:
        """Add a to-do at the bottom of an item's list."""
        text = " ".join(text.split())[:MAX_TODO]
        if not text:
            raise ValueError("A to-do needs some text")
        todo_id = f"todo:{uuid.uuid4().hex[:12]}"
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM items WHERE id = ? AND active = 1", (item_id,)).fetchone():
                raise KeyError(item_id)
            position = db.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM todos WHERE item_id = ?", (item_id,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO todos (id, item_id, text, position, created_at) VALUES (?, ?, ?, ?, ?)",
                (todo_id, item_id, text, position, now_iso()),
            )
        return todo_id

    def edit_todo(self, todo_id: str, text: str) -> None:
        """Change a to-do's text."""
        text = " ".join(text.split())[:MAX_TODO]
        if not text:
            raise ValueError("A to-do needs some text")
        with self._tx() as db:
            if db.execute("UPDATE todos SET text = ? WHERE id = ?", (text, todo_id)).rowcount == 0:
                raise KeyError(todo_id)

    def remove_todo(self, todo_id: str) -> None:
        """Remove a to-do, done or not needed. Either way it leaves the list."""
        with self._tx() as db:
            if db.execute("DELETE FROM todos WHERE id = ?", (todo_id,)).rowcount == 0:
                raise KeyError(todo_id)

    # --- small key/value cache (Jira cloud id, field ids, ...) -----------------

    def meta_get(self, key: str, default=None):
        rows = self._rows("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def meta_set(self, key: str, value) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def meta_delete(self, key: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM meta WHERE key = ?", (key,))


def _end_of(db: sqlite3.Connection, lane: str) -> int:
    sql = "SELECT COALESCE(MAX(position), -1) + 1 FROM items WHERE active = 1 AND lane = ?"
    return db.execute(sql, (lane,)).fetchone()[0]


def _remote_values(item: dict) -> tuple:
    return (
        item.get("kind", ""),
        item.get("ref", ""),
        item["title"],
        item.get("url", ""),
        item.get("status", ""),
        item.get("status_key", ""),
        json.dumps(item.get("roles", [])),
        json.dumps(item.get("info", {})),
        item.get("remote_updated"),
    )


def _decode(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["roles"] = json.loads(item["roles"] or "[]")
    item["info"] = json.loads(item["info"] or "{}")
    return item
