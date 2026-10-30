# Network

What CORSIA listens on, who can reach it, how phones and other devices connect, and what agents can reach going out.

## What listens

| Listener | Address | Port | Who can use it | Source |
|---|---|---|---|---|
| CORSIA server (web app and API) | `127.0.0.1` in a local setup; all interfaces (`0.0.0.0`) in a server setup | 1956 by default | Anyone who can reach the address; API calls need a signed-in session or token, except the routes listed below | `backend/infra/bind_host.py`, `backend/infra/auth.py` |
| Local model server (oMLX), when CORSIA manages one | Follows the CORSIA server's choice | Its own port | Requests need oMLX's own key | `backend/llm/providers/omlx_supervisor_config.py` |
| Devices listener, off unless you turn it on | All interfaces | 47901, or the next free one up to 47910 | Signed-in person sessions only, from private network addresses unless you configure an outside address | `backend/infra/devices_listener.py` |
| Tool-server sign-in callback | `127.0.0.1` | Chosen at sign-in | The sign-in flow that opened it | `backend/tools/mcp_oauth_listener.py` |

**Local or server.** `corsia setup local` binds to `127.0.0.1`, so only programs on the same machine can connect. `corsia setup server` binds to all interfaces, so any device that can reach the machine can reach the sign-in page. The installer chooses local. The desktop app always binds to `127.0.0.1`.

**An install with no machine setup file** (`~/.corsia/config.json`) binds to all interfaces. A setup file with no bind choice is rewritten once with the all-interface bind it already had. An inconsistent file fails closed and names the fix. If you run from source and aren't sure, check with the command below and run `corsia setup local`.

**Routes that take no session or token.** Checked in code at this commit:

| Who can call it | Routes |
|---|---|
| Anyone who can reach the server | `/api/health`, `/api/version`, `/api/auth/status`, `/api/auth/login`, `/api/upload/limits`, `/api/models` (the model list, with account ids and usage), `/api/models/{id}/profile`, `/api/devices/outside-address`, `/api/devices/identity` (signs a caller's challenge to prove this server's certificate key), and the web app's own files |
| Anyone, with a check of its own | Webhooks (`/webhooks/…`): the sender's signature. Device pairing (`/api/devices/pair/…`): only while you have pairing open, with its pairing code. Sign-in callbacks: a matching sign-in you started. The Claude Code tool bridge (`/api/cc/_tool-bridge/{id}`), which lists and runs agent tools: a direct loopback connection plus the token CORSIA issues for that session (`backend/api/cc_attach.py`) |
| Only programs on this machine, through a direct loopback connection | First-run setup routes, which close once an account exists. Also, at any time: whether an update or restart is running (`/api/system/operation-active`), stopping the local model servers CORSIA manages (`/api/system/stop-local-model`), and the corrections classifier's status and download (`/api/setup/classifier…`) |

**Loopback is not a trust boundary for agents.** Agent shells run on the same machine, so they can reach `127.0.0.1:1956`. They have no token, so every other route refuses them. The machine-only routes above don't need one: an agent shell can call them, including stopping CORSIA's local model servers (`backend/api/setup.py`, `backend/api/system.py`, `backend/infra/local_connection.py`).

## Other devices and remote access

| Way | What it does | What to know |
|---|---|---|
| A private tailnet (for example `tailscale serve --bg http://127.0.0.1:1956`) | Gives your devices an HTTPS address that reaches the local server | Only devices on your tailnet can reach it. Agents can walk you through it (`backend/prompts/system/v1/skills/conductor-bootstrap/references/remote-access.md`); CORSIA doesn't check how your tailnet is configured |
| **Devices** pairing | A separate TLS listener with a certificate CORSIA generates (`backend/infra/devices_identity.py`); it announces itself on the local network | Only signed-in person sessions; only private addresses unless you set an outside address; managed only from the main computer |
| A server setup on a network you don't control | Anyone on that network can reach the sign-in page | Sign-in is password only, with growing delays after failures and no second factor. Don't do this |

## Outbound

Outbound network access is **not limited**. Agent shells, the `web_fetch` tool, the agents' browser and tool servers can connect to any address: the internet, your local network (printers, routers, other machines' admin pages) and services on the same machine. Nothing inspects or filters what is sent.

On Linux the sandbox has no network namespace, so the same is true there.

This is the main reason an agent that has been tricked is dangerous: anything it can read, it can send. See [Untrusted content](untrusted-content.md) and [Known limits](limits.md).

## Check it yourself

On macOS, in Terminal (run for this page):

```bash
netstat -an -p tcp | grep LISTEN
```

Look for port 1956. `127.0.0.1.1956` means only this machine can connect; `*.1956` means every interface. A `*.47901` line means the Devices listener is on.

```bash
curl -s http://127.0.0.1:1956/api/health
```

Answers `{"status":"ok"}` without sign-in, and nothing else.
