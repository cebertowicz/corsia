# Accounts and access

How people and programs sign in to CORSIA, how agents act, and what a project or an account does and doesn't separate.

**In short:** CORSIA is built for one person per install. Sign-in protects the server from other devices and other people on the network. It doesn't separate agents from each other, projects from each other, or accounts on the same install from each other.

## Signing in

| | How it works | Source |
|---|---|---|
| Password | At least 8 characters. Stored as a salted scrypt hash (N=2^17, r=8, p=1) | `backend/infra/passwords.py`, `backend/infra/account_validation.py` |
| Wrong passwords | Three free attempts per username, then a delay of 5 seconds that doubles with each failure up to 5 minutes; forgotten after an hour without failures | `backend/api/auth.py` |
| Second factor | None | — |
| Session | A random token, valid for 30 days. The server keeps only its SHA-256 digest. The web app keeps the token in the browser's local storage | `backend/infra/accounts.py`, `frontend/src/lib/stores/auth.ts` |
| Sign-out | Revokes that session's token | `backend/infra/accounts.py` |
| Password change | Ends every browser session of that account; API tokens stay valid | `backend/infra/accounts.py` |
| First run | Before the first account exists, setup routes accept any program on the same computer; they close once the account is created | `backend/infra/local_connection.py` |

Routes take the token in one of three ways. Some accept a `?token=` query parameter or an `Authorization: Bearer` header, and answer an unknown token and an expired one the same way, "Invalid or expired session". Many others, including core thread and file routes, take only the query parameter; `GET /api/threads` is one, and it answers an unknown or expired token with a bare 401. Sign-out and the routes built on `require_user_header_only` (`backend/infra/auth.py`) take only the header. The server's access log drops query strings, so tokens don't reach the log.

**Local storage** is readable by any script running on the CORSIA page. CORSIA serves its own page only; a cross-site scripting flaw in it would expose the session token.

## API tokens

`corsia token create` issues an API token for scripts and outside tools. API tokens **don't expire**. Revoke one with `corsia token revoke <label>`. `corsia token list` shows them, without their values.

An API token can do almost everything a signed-in person can. A few actions accept only a browser session from the person: approving a connected app or changing its reach, managing paired devices, host-access approval settings, and turning Local only on or off. Those checks are on the API; a leased command is a [separate case](host-access.md#approval-modes). Anyone holding an API token can still read that account's conversations, start and message agents, and through them use its connected apps.

`corsia token create --write-delegate-file` writes the token to `~/.corsia/credentials/api-token`, readable only by your user. Agent shells can't read `~/.corsia`, and that folder stays closed even to a shell running under a host-access lease. A token from an earlier version, at `~/.corsia/.api-token`, moves there the next time a `corsia` command reads or writes it.

`CORSIA_API_TOKEN_FILE` points the `corsia` commands at a file you choose instead. That file is outside the closed folder, so a leased shell may be able to read it.

## How agents act

Agents don't sign in. CORSIA's tools run inside the server, which knows which conversation called them. An agent's shell has no CORSIA token, so from it `127.0.0.1:1956` answers only the routes that take none, listed in [Network](network.md#what-listens), the machine-only ones included.

What an agent can reach through tools:

| | Reach |
|---|---|
| Files | The whole workspace and registered repositories ([Agent sandbox](agent-sandbox.md)) |
| Other conversations | Any conversation's messages by its id, in any project, except an [isolated worker's](untrusted-content.md#isolated-workers) (`backend/tools/query_projects.py`) |
| Connected apps | Those whose reach includes its project ([Connected apps](connected-apps.md)) |
| Host access | Only through a reviewed lease ([Host access](host-access.md)) |
| CORSIA's own API | The CORSIA agent and project conductors only. Their `corsia_api` tool calls API routes inside the server with no token, as the account, and the tool doesn't hold a conductor to its project. Every route is open except a person-only list: sign-in and tokens, host-access approval, stored keys, answering your decisions, naming conductors, account sign-ins, webhooks, and handlers that record you as the actor (`backend/tools/conductor_api_policy.py`) |

## What a project does and doesn't separate

A **project** decides what an agent is given by default: its project's context and instructions, which connected apps it can use, and whether Local only applies. It does **not** stop an agent from reading or changing another project's files, or reading another project's conversations.

To keep two bodies of work apart, give each its own workspace and CORSIA install, ideally under separate macOS users.

## More than one account

`corsia user add` can create more accounts on one install. Their conversations and settings are separate rows in one database, but every account's agents run as the same operating-system user, in the same workspace. An agent working for one account can read files written for another, and can read another account's conversations by id. **Accounts are not a security boundary.** Use one install per person.

## Check it yourself

From an agent's shell, ask it to run:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:1956/api/threads
```

`422` means the request was refused for having no token (checked for this page).
