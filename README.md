# workhub

One board for your Jira tickets and your GitLab issues, MRs and review requests.
Plan the day, keep one priority list, and always know what to pick up next.

## Setup

```sh
ln -s ~/scripts/workhub/workhub ~/.local/bin/workhub
cp config.example.toml ~/.config/workhub/config.toml
workhub jira-token # paste your Atlassian API token once
workhub board # start the board server
```

The board lives at http://localhost:8765/. Open it in Zen once and right-click the tab:
"Add to Essentials" keeps it in every workspace, "Pin Tab" keeps it in the current one.
`[board] window` in the config picks what `workhub board` opens: `none` (nothing, use the pinned tab),
`browser` (a new tab in your default browser) or `app` (a separate Edge window).

GitLab is read through `glab api`, so it uses your existing glab login.
Jira needs an API token from https://id.atlassian.com/manage-profile/security/api-tokens.
A scoped token needs the Jira scopes `read:jira-work` and `read:jira-user`. `workhub jira-token`
stores it in `~/.config/workhub/jira-token` with mode 600 and tests it.

## How the board works

| Lane    | Meaning |
|---------|---------|
| Inbox   | New from Jira or GitLab, not sorted yet |
| Today   | Your plan for today, top first |
| Up next | Your priority list, top first |
| Waiting | Blocked, or waiting on someone else |
| Later   | Parked |

- Drag cards between lanes and up or down to set the order.
- The badge shows the status in Jira or GitLab (Open, Issue Clarification, Preparation,
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
- Data: `~/.local/share/workhub/hub.db` (SQLite)
- Server log: `~/.local/state/workhub/server.log`

The server listens on 127.0.0.1:8765 only and refuses requests from other sites.
