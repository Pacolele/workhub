# workhub

One board for your Jira tickets and your GitLab issues, MRs and review requests.
Plan the day, keep one priority list, and always know what to pick up next.

## Requirements

- Linux or macOS
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs Python 3.11+ on first run
  (workhub has no other dependencies)
- For GitLab: the [`glab`](https://gitlab.com/gitlab-org/cli) CLI, logged in to your GitLab host
- For Jira: a Jira Cloud account and an Atlassian API token
- A browser for the board. `[board] window = "app"` needs Edge or Chrome.

## Install

```sh
git clone git@github.com:Pacolele/workhub.git ~/scripts/workhub
mkdir -p ~/.local/bin ~/.config/workhub
ln -s ~/scripts/workhub/workhub ~/.local/bin/workhub      # make sure ~/.local/bin is on your PATH
cp ~/scripts/workhub/config.example.toml ~/.config/workhub/config.toml
$EDITOR ~/.config/workhub/config.toml                     # fill in your GitLab host and Jira site
glab auth login --hostname gitlab.example.com             # once, if glab is not logged in yet
workhub jira-token                                        # paste your Atlassian API token once
workhub board                                             # start the board server
```

The board lives at http://localhost:8765/. Open it in your browser once and pin the tab
(in Zen: right-click the tab, "Add to Essentials" or "Pin Tab").
`[board] window` in the config picks what `workhub board` opens: `none` (nothing, use the pinned tab),
`browser` (a new tab in your default browser) or `app` (a separate Edge or Chrome window).

Tokens:

- GitLab is read through `glab api`, so workhub uses your existing glab login and never sees the token.
- Jira needs an API token from https://id.atlassian.com/manage-profile/security/api-tokens.
  A scoped token needs the Jira scopes `read:jira-work` and `read:jira-user`.
  `workhub jira-token` stores it in `~/.config/workhub/jira-token` with mode 600 and tests it.
  The environment variable `JIRA_API_TOKEN` overrides the file.

## Configuration

The config is a TOML file at `~/.config/workhub/config.toml`. Set `WORKHUB_CONFIG` to use another path.
Restart the board server (`workhub stop`, then `workhub board`) after you change it.

```toml
[gitlab]
host = "gitlab.example.com"
groups = ["my-group/my-subgroup"]

[jira]
site = "https://your-site.atlassian.net"
email = "you@example.com"
token_file = "~/.config/workhub/jira-token"
jql = '''
(assignee = currentUser() OR "Dev Engineer" = currentUser())
AND status in ("Open", "Reopened", "Development")
'''
role_fields = ["Dev Engineer"]
api_base = ""

[board]
port = 8765
window = "none"
sync_minutes = 10
```

| Section and key | Meaning |
|-----------------|---------|
| `[gitlab] host` | GitLab host name that `glab` is logged in to (required in this section) |
| `[gitlab] groups` | Groups to search, subgroups included (required, one or more). `group = "x"` also works for one |
| `[jira] site` | Your Jira Cloud URL, must start with `https://` (required in this section) |
| `[jira] email` | Your Atlassian account email (required in this section) |
| `[jira] jql` | Which tickets appear. A ticket that stops matching (closed, reassigned) leaves the board (required in this section) |
| `[jira] role_fields` | User fields that put a role badge on the card when they name you |
| `[jira] token_file` | Where the token is kept. Default: `~/.config/workhub/jira-token` |
| `[jira] api_base` | Leave empty. workhub finds out whether your token needs the `api.atlassian.com` gateway (scoped tokens do) |
| `[board] port` | Local port, 1024 to 65535. Default: 8765 |
| `[board] window` | `none`, `browser` or `app`. Default: `app` |
| `[board] sync_minutes` | How often the running board syncs. Default: 10 |

### Sources you can add

Each source is optional. Leave out a section to turn that source off.

| Source | Config section | What it shows |
|--------|----------------|---------------|
| Jira Cloud | `[jira]` | Every ticket your JQL matches |
| GitLab | `[gitlab]` | Open issues assigned to you, and open MRs where you are author, assignee or reviewer, in the listed groups |
| Daily tasks | none (always on) | Tasks you add yourself with `workhub add` or the box on the board |

Examples:

- Only GitLab: keep `[gitlab]` and delete `[jira]`.
- Only Jira: keep `[jira]` and delete `[gitlab]`.
- Several GitLab groups: `groups = ["team-a", "team-b/tools"]`. All groups must be on the same `host`.
- Different tickets: change the JQL. For example, `assignee = currentUser() AND statusCategory != Done`.
- A new status: add its name to the `status in (...)` list in the JQL (for example `"Reopened"`).
  The badge colour comes from the status name, and unknown names get the default grey badge.

Jira Server and Data Center, GitHub, and other trackers are not supported. Sources are Python
modules (`src/workhub/jira.py`, `src/workhub/gitlab.py`) that return a list of items, so adding
another one means writing a new module, listing it in `src/workhub/sync.py`, and adding its name to `SOURCE_NAMES` in `src/workhub/cli.py` and `SOURCE` in `src/workhub/static/app.js`.

## How the board works

| Lane    | Meaning |
|---------|---------|
| Inbox   | New from Jira or GitLab, not sorted yet |
| Today   | Your plan for today, top first |
| Up next | Your priority list, top first |
| Waiting | Blocked, or waiting on someone else |
| Later   | Parked |

- Drag cards between lanes and up or down to set the order.
- The badge shows the status in Jira or GitLab (Open, Reopened, Issue Clarification, Preparation,
  Development, Validate, Resolved; MR merge state; your review state). Resolved sub-tasks are left out.
- When a ticket is Closed, an MR is merged, or an item is no longer yours, it leaves the board.
  If it comes back within 24 hours it returns to its old lane; later than that it lands in Inbox.
- **Next** is the top card of Today, or of Up next when Today is empty.
- The board syncs every 10 minutes while the server runs. The Sync button syncs now.
- Clicking a reference opens it in your default browser. ✎ adds a note.
- Daily tasks: type one in the box at the top and press Enter. It goes to Today, or to the lane picked
  next to the box. Daily tasks get notes and to-dos like any card, and 🗑 deletes them (it asks first
  when a note or to-dos would be deleted too). They stay until you delete them.
- +☐ adds to-dos to a card (Enter adds the next one, Esc closes). Click a to-do's text to edit it
  (Enter or clicking elsewhere saves, Esc cancels). Tick a to-do when it is done or
  ✕ it when it does not apply; either way it leaves the list. `workhub` and `workhub next` show them too.

## Commands

| Command | What it does |
|---------|--------------|
| `workhub` | Today's plan, the next item, and sync status |
| `workhub next [--short]` | The next item; `--short` prints one line for a tmux status bar |
| `workhub open [REF]` | Open the next item, or `PROJ-123`, `project#253`, `project!278` |
| `workhub list [LANE]` | Every lane in the terminal |
| `workhub add "TEXT [link]" [--lane LANE]` | Add a daily task (to Today unless `--lane` says otherwise) |
| `workhub sync` | Sync now |
| `workhub board` / `workhub stop` | Start the board server (and open the board, per `window`) / stop the server |
| `workhub serve` | Run the server in the foreground (for debugging) |

## Files

- Config: `~/.config/workhub/config.toml` (JQL, GitLab groups, port, window mode)
- Jira token: `~/.config/workhub/jira-token`
- Data: `~/.local/share/workhub/hub.db` (SQLite)
- Server log: `~/.local/state/workhub/server.log`

The server listens on 127.0.0.1:8765 only and refuses requests from other sites.
