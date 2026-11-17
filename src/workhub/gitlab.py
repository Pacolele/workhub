"""GitLab source: your open issues, your MRs and MRs waiting for your review, in the configured groups.

Reads through `glab api`, so it uses the login glab already has and never handles the token.
"""

from __future__ import annotations

import json
import subprocess
from urllib.parse import quote

from .config import GitLabConfig
from .errors import SourceError

PER_PAGE = 100
MAX_PAGES = 20
TIMEOUT = 60

# detailed_merge_status of your own MR -> (card label, colour group)
MERGE_STATUS = {
    "mergeable": ("Ready to merge", "mr-ready"),
    "not_approved": ("Needs approval", "mr-waiting"),
    "discussions_not_resolved": ("Unresolved threads", "mr-attention"),
    "requested_changes": ("Changes requested", "mr-attention"),
    "conflict": ("Merge conflicts", "mr-attention"),
    "need_rebase": ("Needs rebase", "mr-attention"),
    "ci_must_pass": ("Pipeline must pass", "mr-attention"),
    "ci_still_running": ("Pipeline running", "mr-waiting"),
    "blocked_status": ("Blocked by another MR", "mr-attention"),
    "jira_association_missing": ("Jira link missing", "mr-attention"),
    "commits_status": ("Source branch missing", "mr-attention"),
    "checking": ("Checking", "mr-waiting"),
    "unchecked": ("Not checked yet", "mr-waiting"),
    "preparing": ("Preparing", "mr-waiting"),
    "approvals_syncing": ("Checking approvals", "mr-waiting"),
}

# Your reviewer state on someone else's MR -> (card label, colour group)
REVIEW_STATE = {
    "unreviewed": ("Review requested", "review-todo"),
    "review_started": ("Review started", "review-todo"),
    "unapproved": ("Approval removed", "review-todo"),
    "reviewed": ("You reviewed", "review-done"),
    "requested_changes": ("You requested changes", "review-done"),
    "approved": ("You approved", "review-done"),
}


def fetch(cfg: GitLabConfig) -> list[dict]:
    me = _get(cfg.host, "user")
    uid = me.get("id") if isinstance(me, dict) else None
    if not isinstance(uid, int):
        raise SourceError("GitLab did not say who you are (GET /user)")

    items: dict[str, dict] = {}
    for group in cfg.groups:
        base = f"groups/{quote(group, safe='')}"
        for issue in _get_all(cfg.host, f"{base}/issues?state=opened&scope=all&assignee_id={uid}"):
            item = _issue(issue, group)
            items[item["id"]] = item

        mrs: dict[int, tuple[dict, set[str]]] = {}
        for role, param in (("Author", "author_id"), ("Assignee", "assignee_id"), ("Reviewer", "reviewer_id")):
            for mr in _get_all(cfg.host, f"{base}/merge_requests?state=opened&scope=all&{param}={uid}"):
                mrs.setdefault(mr["id"], (mr, set()))[1].add(role)
        for mr, roles in mrs.values():
            review_state = _my_review_state(cfg.host, mr, uid) if roles == {"Reviewer"} else None
            item = _merge_request(mr, roles, review_state, group)
            items[item["id"]] = item
    return list(items.values())


def _get(host: str, path: str):
    try:
        proc = subprocess.run(
            ["glab", "api", "--hostname", host, path], capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except FileNotFoundError as e:
        raise SourceError("glab is not installed (needed to read GitLab)") from e
    except subprocess.TimeoutExpired as e:
        raise SourceError(f"GitLab did not answer within {TIMEOUT}s ({path.split('?')[0]})") from e
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        detail = lines[-1] if lines else f"exit code {proc.returncode}"
        raise SourceError(f"glab api {path.split('?')[0]} failed: {detail}. Check `glab auth status --hostname {host}`")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise SourceError(f"glab api {path.split('?')[0]} did not return JSON") from e


def _get_all(host: str, path: str) -> list[dict]:
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        batch = _get(host, f"{path}&per_page={PER_PAGE}&page={page}")
        if not isinstance(batch, list):
            raise SourceError(f"glab api {path.split('?')[0]} returned {type(batch).__name__}, expected a list")
        out.extend(batch)
        if len(batch) < PER_PAGE:
            return out
    raise SourceError(f"More than {MAX_PAGES * PER_PAGE} results for {path.split('?')[0]}; not syncing a partial list")


def _my_review_state(host: str, mr: dict, uid: int) -> str | None:
    reviewers = _get(host, f"projects/{mr['project_id']}/merge_requests/{mr['iid']}/reviewers")
    for reviewer in reviewers if isinstance(reviewers, list) else []:
        if (reviewer.get("user") or {}).get("id") == uid:
            return reviewer.get("state")
    return None


def _issue(issue: dict, group: str) -> dict:
    labels = issue.get("labels") or []
    status = _scoped(labels, "status")
    kind_label = _scoped(labels, "type")
    ref = _short_ref(issue, group)
    return {
        "id": f"gitlab:issue:{issue['id']}",
        "kind": "Issue",
        "ref": ref,
        "title": issue.get("title") or ref,
        "url": issue.get("web_url", ""),
        "status": _human(status) if status else "Open",
        "status_key": f"gl-{status}" if status else "gl-open",
        "roles": ["Assignee"],
        "info": {
            "project": ref.split("#")[0],
            "due": issue.get("due_date"),
            "tags": [kind_label] if kind_label else [],
        },
        "remote_updated": issue.get("updated_at"),
    }


def _merge_request(mr: dict, roles: set[str], review_state: str | None, group: str) -> dict:
    ref = _short_ref(mr, group)
    info: dict = {"project": ref.split("!")[0], "branch": mr.get("source_branch")}
    if roles == {"Reviewer"}:
        kind = "Review"
        status, key = REVIEW_STATE.get(review_state or "unreviewed", (_human(review_state or ""), "review-todo"))
        info["author"] = (mr.get("author") or {}).get("name")
        shown_roles = ["Reviewer"]
    else:
        kind = "MR"
        merge_status = mr.get("detailed_merge_status") or ""
        if mr.get("draft"):
            status, key = "Draft", "mr-draft"
        else:
            status, key = MERGE_STATUS.get(merge_status, (_human(merge_status) or "Open", "mr-waiting"))
        shown_roles = ["Author"] if "Author" in roles else ["Assignee"]
        if "Reviewer" in roles:
            shown_roles.append("Reviewer")
    return {
        "id": f"gitlab:mr:{mr['id']}",
        "kind": kind,
        "ref": ref,
        "title": mr.get("title") or ref,
        "url": mr.get("web_url", ""),
        "status": status,
        "status_key": key,
        "roles": shown_roles,
        "info": info,
        "remote_updated": mr.get("updated_at"),
    }


def _short_ref(obj: dict, group: str) -> str:
    """my-group/my-subgroup/project#253 -> project#253 when it lives in the configured group."""
    full = (obj.get("references") or {}).get("full") or ""
    prefix = group.rstrip("/") + "/"
    return full[len(prefix) :] if full.startswith(prefix) else full


def _scoped(labels: list[str], scope: str) -> str:
    prefix = f"{scope}::"
    for label in labels:
        if label.startswith(prefix):
            return label[len(prefix) :]
    return ""


def _human(value: str) -> str:
    text = value.replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:]
