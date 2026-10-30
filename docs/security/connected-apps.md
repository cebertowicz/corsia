# Connected apps

What CORSIA can do in the accounts and tools you connect: tool servers, Google, Microsoft and other mail hosts, sending, and the agents' browser.

**In short:** a connected account lets agents read and change what its sign-in allows, inside the projects you give it. Sending email and invites needs your reply in chat first. Most other actions in a connected account don't. A local tool server you add runs as your own program with wide access to your home folder.

## Tool servers

A **tool server** (an MCP server) gives agents extra tools. You add it in **Settings → Connected apps**, either as a program CORSIA starts on your computer or as a web address.

| Kind | How it runs | What it can reach |
|---|---|---|
| Local (a command CORSIA starts) | Inside a macOS sandbox profile that allows by default, in its own working and cache folder (`backend/tools/mcp_local_transport.py`, `backend/tools/safehouse_launch.py`) | Your home folder, **including** the places the agent shell can't read (Documents, `~/.ssh`, cloud credential folders). Refused: CORSIA's state folder (database, key store, the agents' browser profile), CORSIA's private files and the sandbox's own files. Network: not limited |
| Remote (a web address) | CORSIA connects to it; nothing runs on your computer | Only what agents send it in tool calls, plus its own service |

Treat a local tool server like any program you install: it can do what you can, apart from the refusals above. Add only ones you trust. Its keys come from the key store and are passed in its environment when CORSIA starts it ([Secrets](secrets.md)).

On Linux, CORSIA starts local tool servers only through Agent Safehouse, which is macOS only, so they refuse to start (read from code, not run for this page).

**Results from any tool server reach the agent unscreened.** See [Untrusted content](untrusted-content.md).

### Who can use a connected app

| Setting | Effect |
|---|---|
| **All projects** | Every agent in every project, and outside projects |
| **Selected** | Only agents in the projects you pick (and outside projects, if you include them) |

A delegated worker never gets an app its parent can't use. An agent can propose a new app, but it stays **awaiting approval** until you approve it in Settings; an unapproved proposal expires after 7 days (`backend/connections/app_reach.py`). Only a signed-in person can approve an app or change its reach; an agent's attempt is refused and recorded (`backend/api/connected_apps.py`, `connected_app_access.py`).

## Google

Signing in to Google asks for these permissions (`backend/connections/google/oauth.py`):

| | Using CORSIA's published sign-in app | Using your own Google Cloud app |
|---|---|---|
| Gmail (required) | `gmail.modify`: read, compose, send, label and move mail to the trash; not permanent deletion | Same, plus `gmail.send` |
| Calendar (required) | `calendar`: read and change all your calendars | Same |
| Drive and Docs (optional; you can decline them and still connect) | `drive`, `documents`: read and change all your files and documents | Same, plus `spreadsheets` |

The sign-in grants more than CORSIA's tools use. CORSIA's tools send only through the gate below; nothing else in CORSIA uses the grant to send. Labelling, trashing and calendar changes don't need your reply first.

## Microsoft

Signing in to Microsoft grants `Mail.ReadWrite`, `Mail.Send`, `Calendars.ReadWrite`, `Files.ReadWrite`, `User.Read` and `offline_access` (stay signed in). Agents can read and search mail, read and list OneDrive files, and **upload** files to OneDrive without asking you first.

## Other mail hosts

iCloud, Yahoo and similar hosts connect with an app password you create at the host. It is stored in the key store and grants whatever the host gives app passwords, usually full mail access.

## Sending email and invites

CORSIA's email and invite tools send only through a draft you have seen. The send is refused by CORSIA code, not a model, unless all of these hold (`backend/tools/action_gate.py`):

1. The call runs inside a live turn that you started or wrote into.
2. The draft is this conversation's own open draft, and its text is exactly what the draft tool recorded.
3. Your newest message in this turn came after the draft's last change.

**What it doesn't check:** whether your reply said "send". Any reply after the draft counts, including "wait" or a question. The agent is told to send only on your yes, but that is an instruction, not a check.

**What it doesn't cover:**

- Tool servers you add (a Slack or Gmail tool server sends without this gate)
- The agents' browser, if it is signed in to your mail
- Agent shells, which can reach any web service ([Network](network.md))
- Other changes in connected accounts: deleting or labelling mail, changing calendar events you own, uploading files

## The agents' browser

Agents can drive a headless Chrome. Each CORSIA account has one saved browser profile, shared by all of that account's agents (`backend/host_access/browser_launch.py`).

- If you sign in to a site in it, every agent of yours can use that sign-in.
- The profile, which holds its cookies, is in the hard floor: no agent shell can read it, leased or not.
- Pages it visits are not screened.
- It identifies as an ordinary Chrome browser to sites.

Sign out of anything agents shouldn't use, or don't sign in from the agents' browser.

## Check it yourself

- **Settings → Connected apps** lists each app, its reach and any awaiting approval.
- For Google, [myaccount.google.com/permissions](https://myaccount.google.com/permissions) lists what you granted; remove CORSIA there to revoke it.
- For Microsoft, [myapps.microsoft.com](https://myapps.microsoft.com) (work or school) or [account.live.com/consent/Manage](https://account.live.com/consent/Manage) (personal) does the same.
