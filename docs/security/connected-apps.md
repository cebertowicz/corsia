# Connected apps

What CORSIA can do in the accounts and tools you connect: tool servers, Google, Microsoft and other mail hosts, Messages, GitHub, sending, and the agents' browser.

**In short:** a connected account lets agents read and change what its sign-in allows, inside the projects you give it. Sending email, invites and texts through CORSIA's mail, calendar and Messages tools needs your reply in chat first; the `google_workspace` tool can share a file or email a calendar event's guests without asking. Most other actions in a connected account don't. A local tool server you add runs as your own program with wide access to your home folder.

## Tool servers

A **tool server** (an MCP server) gives agents extra tools. You add it in **Settings → Connected apps**, either as a program CORSIA starts on your computer or as a web address.

| Kind | How it runs | What it can reach |
|---|---|---|
| Local (a command CORSIA starts) | Inside a macOS sandbox profile that allows by default, in its own working and cache folder (`backend/tools/mcp_local_transport.py`, `backend/tools/safehouse_launch.py`) | Your home folder, **including** the places the agent shell can't read (Documents, `~/.ssh`, cloud credential folders). Refused: CORSIA's state folder (database, key store, the agents' browser profile), CORSIA's private files and the sandbox's own files. Network: not limited |
| Remote (a web address) | CORSIA connects to it; nothing runs on your computer | Only what agents send it in tool calls, plus its own service |

Treat a local tool server like any program you install: it can do what you can, apart from the refusals above. Add only ones you trust. Its keys come from the key store and are passed in its environment when CORSIA starts it ([Secrets](secrets.md)).

On Linux, CORSIA starts local tool servers and Google Workspace only through Agent Safehouse, which is macOS only, so they refuse to start (read from code, not run for this page).

**Results from any tool server reach the agent unscreened.** See [Untrusted content](untrusted-content.md).

### Who can use a connected app

| Setting | Effect |
|---|---|
| **All projects** | Every agent in every project, and outside projects |
| **Selected** | Only agents in the projects you pick (and outside projects, if you include them) |

A delegated worker never gets an app its parent can't use. An agent can propose a new app, but it stays **awaiting approval** until you approve it in Settings; an unapproved proposal expires after 7 days (`backend/connections/app_reach.py`). Only a signed-in person can approve an app or change its reach; an agent's attempt is refused and recorded (`backend/api/connected_apps.py`, `connected_app_access.py`). That check is on the API; a leased command is a [separate case](host-access.md#approval-modes).

## Google

Signing in to Google asks for these permissions (`backend/connections/google/oauth.py`):

| | Using CORSIA's published sign-in app | Using your own Google Cloud app |
|---|---|---|
| Gmail (required) | `gmail.modify`: read, compose, send, label and move mail to the trash; not permanent deletion | Same, plus `gmail.send` |
| Calendar (required) | `calendar`: read and change all your calendars | Same |
| Drive and Docs (optional; you can decline them and still connect) | `drive`, `documents`: read and change all your files and documents | Same, plus `spreadsheets` |

The sign-in grants more than CORSIA's tools use. Labelling, trashing and calendar changes don't need your reply first.

### Docs, Sheets, Drive and Calendar

The `google_workspace` tool runs one command of Google's Workspace CLI (`gws`) on a connected account (`backend/tools/google_workspace.py`). CORSIA previews each command without the sign-in, checks the one request it would make, then runs it with the account's access token handed only to that `gws` process ([Secrets](secrets.md#where-keys-live)). The check, in CORSIA code (`backend/tools/gws_check.py`):

- refuses mail (that goes through the email tools), sign-in commands, any address outside Google's APIs, emptying the trash, and any delete that can't be put back
- turns a Drive delete into a move to the trash, and saves a calendar event before a change that would remove it
- reads and writes local files only inside the workspace

It doesn't ask you first. An agent can create, edit and share files, and change calendar events, including ones that email their guests. Edits appear as yours. On Linux the tool refuses to run, because `gws` starts only under the macOS sandbox.

## Microsoft

Signing in to Microsoft grants `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`, `Files.ReadWrite`, `User.Read` and `offline_access` (stay signed in). Agents can read and search mail, read and list OneDrive files, and **upload** files to OneDrive without asking you first.

## Other mail hosts

iCloud, Yahoo and similar hosts connect with an app password you create at the host. It is stored in the key store and grants whatever the host gives app passwords, usually full mail access.

## Messages

On a Mac, agents can read, search and send your texts (iMessage and SMS) through the Messages app (`backend/tools/messages_tools.py`, `messages_send.py`).

- The CORSIA agent adds Messages to **Settings → Connected apps**; it stays off until you press **Enable** there, and reaches only the projects you choose, like any app.
- Reading needs **Full Disk Access** for CORSIA (**System Settings → Privacy & Security → Full Disk Access**). That grant is for the whole app: once it is on, local tool servers and leased commands can also read every file macOS protects, such as Mail, Messages and Photos ([Host access](host-access.md#what-no-lease-opens)).
- Sending needs macOS's permission for CORSIA to control Messages (**Automation → Messages**), and goes through the send gate below.
- Texts reach the agent unscreened ([Untrusted content](untrusted-content.md)).

To stop it, switch CORSIA off under Full Disk Access and Automation.

## GitHub

You can give agents their own GitHub account, separate from yours. Its GitHub CLI login sits in its own folder, which agent shells can read; once CORSIA has checked that it isn't your login, agent shells' `git push` and `gh` run as that account, and your own GitHub CLI login is hidden from them (`backend/connections/github_agent.py`, [Agent sandbox](agent-sandbox.md)). Without one, agent shells use your SSH agent and your GitHub CLI login ([Limits](limits.md)).

## Sending email and invites

CORSIA's email, invite and text-message tools send only the conversation's open draft, unchanged, and only after you have written in the conversation since its last change. CORSIA can't tell whether you read the draft. The send is refused by CORSIA code, not a model, unless all of these hold (`backend/tools/action_gate.py`):

1. The call runs inside a live turn that you started or wrote into.
2. The draft is this conversation's own open draft, and its text is exactly what the draft tool recorded.
3. Your newest message in this turn came after the draft's last change.

**What it doesn't check:** whether your reply said "send". Any reply after the draft counts, including "wait" or a question. The agent is told to send only on your yes, but that is an instruction, not a check.

**What it doesn't cover:**

- Tool servers you add (a Slack or Gmail tool server sends without this gate)
- The `google_workspace` tool: sharing a file, or a calendar change that emails its guests
- The agents' browser, if it is signed in to your mail
- Agent shells, which can reach any web service ([Network](network.md))
- Other changes in connected accounts: deleting or labelling mail, changing calendar events you own, uploading files

## The agents' browser

Agents can drive a headless Chrome. Each CORSIA account has one saved browser profile, shared by all of that account's agents (`backend/host_access/browser_launch.py`).

- If you sign in to a site in it, every agent of yours can use that sign-in.
- The profile, which holds its cookies, is in the hard floor: no agent shell can read it directly, leased or not ([the indirect route](host-access.md#what-no-lease-opens)).
- Pages it visits are not screened.
- It identifies as an ordinary Chrome browser to sites.

Sign out of anything agents shouldn't use, or don't sign in from the agents' browser.

## Check it yourself

- **Settings → Connected apps** lists each app, its reach and any awaiting approval.
- For Google, [myaccount.google.com/permissions](https://myaccount.google.com/permissions) lists what you granted; remove CORSIA there to revoke it.
- For Microsoft, [myapps.microsoft.com](https://myapps.microsoft.com) (work or school) or [account.live.com/consent/Manage](https://account.live.com/consent/Manage) (personal) does the same.
