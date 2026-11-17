"""Pull every configured source into the store."""

from __future__ import annotations

import logging
import threading

from . import gitlab, jira
from .config import Config
from .errors import SourceError
from .store import Store, now_iso

log = logging.getLogger(__name__)
_one_at_a_time = threading.Lock()


def run(cfg: Config, store: Store) -> dict[str, dict]:
    """Sync all sources. A source that fails keeps its items exactly as they were.

    Returns {source: {"count", "new", "finished", "returned"}} or {source: {"error"}}.
    """
    jobs = []
    if cfg.gitlab:
        jobs.append(("gitlab", lambda: gitlab.fetch(cfg.gitlab)))
    if cfg.jira:
        jobs.append(("jira", lambda: jira.fetch(cfg.jira, store)))

    report: dict[str, dict] = {}
    with _one_at_a_time:
        for name, fetch in jobs:
            started = now_iso()
            try:
                items = fetch()
            except SourceError as e:
                error = str(e)
            except Exception as e:  # a bug must not stop the other source or the board
                log.exception("Sync of %s crashed", name)
                error = f"Unexpected error: {e}"
            else:
                for item in items:
                    item["source"] = name
                changes = store.apply_sync(name, items, started)
                store.record_source(name, started, count=len(items))
                report[name] = {"count": len(items), **changes}
                continue
            store.record_source(name, started, error=error)
            report[name] = {"error": error}
    return report
