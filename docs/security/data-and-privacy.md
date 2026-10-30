# Data and privacy

What leaves your machine, to whom and when; what **Local only** does and doesn't do; telemetry; what's stored on disk; and how to remove everything.

## What leaves your machine

CORSIA runs on your computer. Data leaves it in the cases below. "Agent's choice" means an agent decides at run time, and outbound access is not limited ([Network](network.md)).

| Destination | What is sent | When | How to stop it |
|---|---|---|---|
| Cloud model providers you connect (API providers, and the Claude Code and Codex command-line tools, which call their vendors) | Each turn's input: instructions and context files, the conversation, tool results (credentials masked, see [Secrets](secrets.md)), and the contents of files and pages the agent read | Every turn on a cloud model | Use local models; turn on **Local only** (below); turn a provider off in **Settings → Providers** |
| A local model server on this machine | The same, but it stays on the machine | Every turn on a local model | — |
| Web search: Brave with your key, otherwise a keyless search library that queries public search engines | The search text | When an agent searches | Don't give agents a reason to search; there is no setting that removes the tool |
| Any website | Whatever the agent puts in the request | Agent's choice, through the `web_fetch` tool, the agents' browser or the shell | Not limited |
| Your email, calendar and file providers (Google, Microsoft, mail hosts) | Reads, drafts, changes, and sends that passed the send gate ([Connected apps](connected-apps.md#sending-email-and-invites)) | When an agent uses those tools | Disconnect the account |
| Tool servers you add | What the agent passes to the tool | When an agent calls the tool | Remove the app |
| GitHub, if you connect it | Pull requests, comments, pushes | When an agent uses it | Disconnect it |
| GitHub, for update checks | An anonymous request: `git fetch` of your branch's upstream for a source install; the release manifest for the desktop app | While the app is open, at most once every five minutes | No setting at this commit |
| GitHub, for LiteLLM's model price list | An anonymous download of a public JSON file by the LiteLLM library | Each time the server starts | Set `LITELLM_LOCAL_MODEL_COST_MAP=true` in the server's environment |
| GitHub, for the skill library index | An anonymous download of the public index | Daily, and when an agent searches or installs | No setting at this commit |
| GitHub, for the corrections classifier | A one-time download of the pinned classifier files, checked against hashes in the source | First run, and when a new version is pinned | Don't install the classifier |
| models.dev | An anonymous download of a public model catalog | When CORSIA discovers models on an OpenAI-compatible endpoint you added | Set `CORSIA_MODEL_CATALOG_URL` to empty |
| Download hosts for setup | Anonymous downloads of pinned runtimes, command-line tools and local models | When you set them up | Don't set them up |

**Not sent:**

- **Product analytics.** The server records product events in a local database on your machine (`backend/infra/product_events.py`). The web app includes an analytics library that sends nothing unless both an analytics key and an analytics host are set when the app is built (`frontend/src/lib/product_events.ts`); neither is set by this repository's build and release scripts. `CORSIA_ANALYTICS_DISABLE=1` turns off the local recording too.
- **Live System bulletins.** The code can fetch signed notices from this repository, but only with signing keys shipped in the release. At this commit none ship, so the feed is off (`backend/infra/bulletins.py`).
- **Crash reports.** None are collected.

## Local only

**Local only** controls which models may run. Nothing else.

| | |
|---|---|
| Where | One chat, in its model settings; or a whole project, where the setting is called **Local models only** |
| Who can change it | Only you, from a signed-in session (`backend/api/thread_mutations.py`); a leased command is a [separate case](host-access.md#approval-modes) |
| What it allows | Models whose every endpoint is a loopback address on this machine, and models on a server you marked **Trusted device** in its provider settings (the mark is yours to set; it isn't checked). An External endpoint counts as local only with that mark, even if it runs on your machine (`backend/llm/locality.py`) |
| Inherited by | Every worker the chat starts, and their workers |
| Messages to other chats | A Local only chat messaging a chat on a cloud model must confirm the message holds nothing sensitive. The agent makes that confirmation: Model judgment |
| Reviewer | The lease reviewer must also be a model the chat may use |

Local only does **not** stop the chat's agent from using the web, email, tool servers, GitHub or the shell's network access. Content those tools send goes where they send it. A Local only chat on a local model with no network-facing tools sends nothing, but nothing in CORSIA enforces that combination.

## What's stored, and where

| What | Where | Encrypted |
|---|---|---|
| Conversations, plans, settings, the audit trail | A SQLite database in CORSIA's state folder | No |
| Keys and sign-in tokens | `keys/values.json` in the state folder ([Secrets](secrets.md)) | No |
| Logs | `logs/` in the state folder | No |
| The agents' browser profile (cookies, logins of sites agents use) | The state folder | No |
| Your context files, skills, documents, each chat's working files | The workspace folder (default `~/corsia`) | No |

The state folder is `~/Library/Application Support/CORSIA/state` for the desktop app on macOS, `~/Library/Application Support/CORSIA/source-state` for a source install on macOS, and `$XDG_STATE_HOME/corsia/state` on Linux (`~/.local/state/corsia/state` when `XDG_STATE_HOME` isn't set). `CORSIA_STATE_DIR` overrides it on every platform (`backend/infra/platform_paths.py`).

**At rest:** CORSIA adds no encryption of its own. Turn on FileVault (macOS) or disk encryption (Linux) to protect the disk when the machine is off or locked. While you are signed in, anything running as your user can read these files.

**Retention:** chats that are not persistent agents become dormant after 14 idle days, archived after 30 more, and their messages are deleted 180 days after archiving. Pinned chats and persistent agents are never purged. Long tool outputs are trimmed after 30 days once a checkpoint covers them. Source: `backend/core/thread_lifecycle.py`. Lease and review records are kept; see [Audit](audit.md).

## Removing everything

1. Stop CORSIA: `corsia stop`. For a source install with the background service, also run `corsia service uninstall` (the desktop app refuses this command; it manages its own service).
2. Delete the state folder: the path above for your platform, or `CORSIA_STATE_DIR` if you set it.
3. Delete the workspace folder (default `~/corsia`).
4. Delete the checkout (`~/.corsia`) or the app.
5. If you set up an agents' GitHub account, delete its folder (default `~/.config/corsia-gh-agent`), which holds a live token, and revoke that account's GitHub CLI access in its GitHub settings.
6. Optional: remove local models from the model folder your local model server reports in its settings; uninstall Agent Safehouse and `agent-browser` if nothing else uses them.
7. Revoke CORSIA's access in your Google or Microsoft account settings, and sign out of any tool servers you connected.

Data you sent to cloud model providers and other services is held under their policies. Deleting CORSIA doesn't remove it.

## Check it yourself

From a terminal on the machine (run for this page at the commit named in the [overview](README.md)):

```bash
grep -n "^PUBLIC_KEYS" backend/infra/bulletins.py   # PUBLIC_KEYS: tuple[str, ...] = (): no notice keys, feed off
grep -n "^PUBLIC_POSTHOG_HOST=" frontend/.env.example   # empty: the template has no analytics host
grep -rn "PUBLIC_POSTHOG" .github desktop scripts      # no output: release and build scripts set no analytics key
```

The web app sends analytics only when both an analytics key and host are set at build time.
