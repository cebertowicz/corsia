# Audit

What CORSIA records about what agents and people did, where it's kept, for how long, and how to read it.

**In short:** every lease, every reviewed command and every change to a connected app or account is recorded in CORSIA's database, with no automatic deletion. Ordinary agent actions are recorded only in the conversation that made them, which ages out. The records aren't tamper-proof: anything running as your user can change them.

## What's recorded

| What | Recorded | Kept | Source |
|---|---|---|---|
| Host-access leases | Who asked, purpose, plan, requested length, current state | No deletion path | `host_access_leases` table, `backend/infra/host_access_leases.py` |
| Each command or action under a lease | The command with credentials masked, the reviewer model, its verdict and reason, and the one-time permit | No deletion path | `host_access_action_reviews` table |
| Connected apps | Each add, approval, reach change and removal, with whether a person, agent or CORSIA did it, and the reach before and after. Refused agent attempts too | No deletion path found | `connected_app_audit` table |
| Google, Microsoft and mail-host accounts | Connect, sign-in, sign-out and removal, with who did it and why | No deletion path found | `connected_account_audit` table |
| Email screen verdicts | One verdict per message; never the mail itself | 90 days | `backend/connections/mail/email_screen.py` |
| Everything an agent did in a conversation: its messages, every tool call and shell command, and the results | The conversation's messages | Long results trimmed after 30 days once a checkpoint covers them; messages deleted 180 days after a chat is archived ([Data and privacy](data-and-privacy.md#whats-stored-and-where)) | `backend/core/thread_lifecycle.py` |
| Server requests | The access log, without query strings, so tokens passed as `?token=` aren't written | Not checked for this page | `backend/infra/access_log.py` |
| CORSIA's own log records | `otel_logs.db` in the state folder | 14 days | `backend/infra/otel_logs/store.py` |

Everything above except the logs is in `corsia.db`, the SQLite database in CORSIA's state folder.

## What isn't recorded

- **Network traffic.** Nothing records what agents send or fetch, beyond the tool calls in their conversations.
- **What a local tool server does** on your computer. Only the tool calls agents make to it are in the conversation.
- **Files an agent read** through the shell, other than the command itself in the conversation.
- **Changes made to the database directly.** Nothing records them.

## Can the record be trusted?

The records are rows in an ordinary, unencrypted database file owned by your user. They aren't signed, chained or copied elsewhere.

- **An agent's shell can't reach them**: the state folder is outside the sandbox ([Agent sandbox](agent-sandbox.md)).
- **A leased command can.** A lease can open the state folder ([Host access](host-access.md#what-no-lease-opens)). The reviewers are told to stop needless destruction, but nothing else stops a leased command from editing or deleting audit rows.
- **Any other program running as your user can**, as can you.

If you need a record an agent can't change, copy it off the machine yourself.

## How to read it

| | How |
|---|---|
| A conversation's history | Open the conversation; tool calls and their results are shown in it |
| Leases waiting or active | Shown in the conversation that asked |
| Lease history, summarized | Ask an agent to call `audit_read` with the `lease_reviews` metric. It counts leases by state, for a window of up to 7 days, for that agent's account and project only; it needs no lease (`backend/tools/audit_reads.py`) |
| Full rows | Open the database yourself with `sqlite3` (below). There's no screen in the app for audit history yet |

## Check it yourself

From Terminal, read-only (desktop app on macOS; see [Data and privacy](data-and-privacy.md#whats-stored-and-where) for other state folders):

```bash
sqlite3 -readonly "$HOME/Library/Application Support/CORSIA/state/corsia.db" \
  "SELECT created_at, tool_name, verdict, reason FROM host_access_action_reviews ORDER BY created_at DESC LIMIT 20;"
```

The last 20 commands reviewed under leases, with each verdict and its reason.
