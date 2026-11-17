"""Jira Cloud source: the tickets your JQL matches (assignee, Dev Engineer, Business Analyst).

Auth is your Atlassian email plus an API token (Basic auth). Scoped tokens only work through
the api.atlassian.com gateway and classic tokens only on the site URL, so the first sync tries
both and remembers which one answered. A token Jira rejects is remembered and not retried until
you save a new one, so a stale token can't rack up failed logins every few minutes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from .config import JiraConfig
from .errors import SourceError
from .store import Store, now_iso

log = logging.getLogger(__name__)

FIELDS = ["summary", "status", "issuetype", "priority", "project", "parent", "assignee", "duedate", "updated"]
PAGE_SIZE = 100
MAX_PAGES = 30
TIMEOUT = 30
FIELD_CACHE = timedelta(days=1)
GATEWAY = "https://api.atlassian.com/ex/jira/{cloud_id}"
TOKEN_HELP = (
    "Run `workhub jira-token` to save one (create it at https://id.atlassian.com/manage-profile/security/api-tokens)."
)
REJECTED_KEY = "jira.rejected_token"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect becomes an HTTPError; the token is never sent anywhere else


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(cfg: JiraConfig, store: Store) -> list[dict]:
    client = Client(cfg, load_token(cfg), store)
    me = client.connect()
    roles = client.role_fields()
    fields = FIELDS + sorted({field_id for ids in roles.values() for field_id in ids})
    issues = client.search(cfg.jql, fields)
    return [_item(issue, me["accountId"], roles, cfg.site) for issue in issues]


def check(cfg: JiraConfig, store: Store) -> dict:
    """Log in once and return /myself. Used by `workhub jira-token`."""
    store.meta_delete(REJECTED_KEY)
    return Client(cfg, load_token(cfg), store).connect()


def load_token(cfg: JiraConfig) -> str:
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if token:
        return token
    path = cfg.token_file
    if not path.is_file():
        raise SourceError(f"No Jira API token yet. {TOKEN_HELP}")
    if path.stat().st_mode & 0o077:
        raise SourceError(f"{path} can be read by other users. Run: chmod 600 {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token:
        raise SourceError(f"{path} is empty. {TOKEN_HELP}")
    return token


class Client:
    def __init__(self, cfg: JiraConfig, token: str, store: Store):
        self.cfg = cfg
        self.store = store
        self.site = cfg.site
        self.base = ""
        self._auth = "Basic " + base64.b64encode(f"{cfg.email}:{token}".encode()).decode()
        self._fingerprint = hashlib.sha256(f"{cfg.email}\0{token}".encode()).hexdigest()[:16]

    def connect(self) -> dict:
        """Find the base URL that accepts the token and return the logged-in user."""
        if self.store.meta_get(REJECTED_KEY) == self._fingerprint:
            raise SourceError(f"Jira rejected this API token earlier, so it isn't retried. {TOKEN_HELP}")
        for base in self._candidates():
            status, data = self._call(base, "GET", "/rest/api/3/myself")
            if status == 200 and isinstance(data, dict) and data.get("accountId"):
                self.base = base
                self.store.meta_set(f"jira.api_base:{self.site}", base)
                return data
            if status in (401, 403, 404):
                log.info("Jira %s answered %s on /myself", base, status)
                continue
            raise SourceError(f"Jira answered HTTP {status} at {base}: {_message(data)}")
        self.store.meta_set(REJECTED_KEY, self._fingerprint)
        raise SourceError(
            f"Jira rejected the API token for {self.cfg.email}. Scoped tokens need the scopes read:jira-work"
            f" and read:jira-user. {TOKEN_HELP}"
        )

    def role_fields(self) -> dict[str, list[str]]:
        """Map role field names ("Dev Engineer") to field ids. Failing here only costs the role badges."""
        names = list(self.cfg.role_fields)
        if not names:
            return {}
        key = f"jira.role_fields:{self.site}"
        cached = self.store.meta_get(key)
        if cached and cached.get("names") == names and _fresh(cached.get("at"), FIELD_CACHE):
            return cached["map"]
        status, data = self._call(self.base, "GET", "/rest/api/3/field")
        if status != 200 or not isinstance(data, list):
            log.warning("Could not list Jira fields (HTTP %s): %s", status, _message(data))
            return cached["map"] if cached else {}
        wanted = {name.casefold(): name for name in names}
        found: dict[str, list[str]] = {name: [] for name in names}
        for field in data:
            name = wanted.get(str(field.get("name", "")).casefold())
            if name and field.get("id"):
                found[name].append(str(field["id"]))
        self.store.meta_set(key, {"names": names, "map": found, "at": now_iso()})
        return found

    def search(self, jql: str, fields: list[str]) -> list[dict]:
        issues: dict[str, dict] = {}
        page_token = None
        for _ in range(MAX_PAGES):
            body: dict = {"jql": jql, "fields": fields, "maxResults": PAGE_SIZE}
            if page_token:
                body["nextPageToken"] = page_token
            status, data = self._call(self.base, "POST", "/rest/api/3/search/jql", body)
            if status != 200 or not isinstance(data, dict):
                raise SourceError(f"Jira search failed (HTTP {status}): {_message(data)}")
            before = len(issues)
            for issue in data.get("issues") or []:
                if isinstance(issue, dict) and issue.get("id"):
                    issues[str(issue["id"])] = issue
            page_token = data.get("nextPageToken")
            # Stop on the last page, and also if a page brings nothing new (a looping token).
            if not page_token or data.get("isLast") is True or len(issues) == before:
                return list(issues.values())
        raise SourceError(f"Your JQL matches more than {MAX_PAGES * PAGE_SIZE} tickets; narrow it in the config")

    def _candidates(self) -> list[str]:
        if self.cfg.api_base:
            return [self.cfg.api_base]
        found = [self.store.meta_get(f"jira.api_base:{self.site}")]
        cloud_id = self._cloud_id()
        if cloud_id:
            found.append(GATEWAY.format(cloud_id=cloud_id))
        found.append(self.site)
        return list(dict.fromkeys(base for base in found if base))

    def _cloud_id(self) -> str:
        key = f"jira.cloud_id:{self.site}"
        cloud_id = self.store.meta_get(key)
        if cloud_id:
            return cloud_id
        try:
            status, data = self._call(self.site, "GET", "/_edge/tenant_info", auth=False)
        except SourceError:
            return ""
        cloud_id = data.get("cloudId", "") if status == 200 and isinstance(data, dict) else ""
        if re.fullmatch(r"[0-9a-fA-F-]{36}", cloud_id or ""):
            self.store.meta_set(key, cloud_id)
            return cloud_id
        return ""

    def _call(self, base: str, method: str, path: str, body: dict | None = None, auth: bool = True):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(base + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if auth:
            request.add_unredirected_header("Authorization", self._auth)
        try:
            with _OPENER.open(request, timeout=TIMEOUT) as response:
                return response.status, _json(response.read())
        except urllib.error.HTTPError as e:
            return e.code, _json(e.read())
        except (urllib.error.URLError, OSError) as e:
            raise SourceError(f"Cannot reach {base}: {getattr(e, 'reason', e)}") from e


def _item(issue: dict, me: str, role_fields: dict[str, list[str]], site: str) -> dict:
    fields = issue.get("fields") or {}
    key = str(issue.get("key", ""))
    status = fields.get("status") or {}
    issue_type = fields.get("issuetype") or {}
    parent = fields.get("parent") or {}
    roles = ["Assignee"] if _is_me(fields.get("assignee"), me) else []
    for name, ids in role_fields.items():
        if any(_is_me(fields.get(field_id), me) for field_id in ids):
            roles.append(name)
    status_name = str(status.get("name") or "")
    return {
        "id": f"jira:{issue['id']}",
        "kind": issue_type.get("name") or "Issue",
        "ref": key,
        "title": fields.get("summary") or key,
        "url": f"{site}/browse/{key}",
        "status": status_name,
        "status_key": re.sub(r"[^a-z0-9]+", "-", status_name.lower()).strip("-"),
        "roles": roles,
        "info": {
            "project": (fields.get("project") or {}).get("key"),
            "priority": (fields.get("priority") or {}).get("name"),
            "due": fields.get("duedate"),
            "parent": parent.get("key"),
            "parent_title": (parent.get("fields") or {}).get("summary"),
            "status_cat": (status.get("statusCategory") or {}).get("key"),
        },
        "remote_updated": fields.get("updated"),
    }


def _is_me(value, me: str) -> bool:
    if isinstance(value, dict):
        return value.get("accountId") == me
    if isinstance(value, list):
        return any(_is_me(v, me) for v in value)
    return False


def _json(raw: bytes):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _message(data) -> str:
    if isinstance(data, dict):
        parts = [str(m) for m in data.get("errorMessages") or []]
        parts += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
        if data.get("message"):
            parts.append(str(data["message"]))
        if parts:
            return "; ".join(parts)[:400]
    return "no details"


def _fresh(when: str | None, max_age: timedelta) -> bool:
    try:
        return bool(when) and datetime.fromisoformat(now_iso()) - datetime.fromisoformat(when) < max_age
    except ValueError:
        return False
