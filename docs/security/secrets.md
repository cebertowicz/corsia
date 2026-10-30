# Secrets

Where CORSIA keeps keys, who can read them, how credential values are kept away from models, and what happens to a key that ends up in a file.

## Where keys live

| What | Where | Readable by |
|---|---|---|
| Provider API keys, the web search key, connected-account tokens (Google, Microsoft, mail hosts), tool-server keys and their sign-in grants | The **key store**: one file, `keys/values.json`, in CORSIA's state folder | The CORSIA server process, and anything else running as your user outside CORSIA |
| Names, labels and dates of those keys | CORSIA's database | The server; the person in **Settings → Keys** |
| Claude Code and Codex logins | Those tools' own folders (`~/.claude`, `~/.codex`) | Those tools; not agent shells |
| Your session and API tokens | Only a SHA-256 digest is stored; see [Accounts and access](accounts-and-access.md) | — |

The key store file is created with mode `0600` in a `0700` folder. **It is not encrypted.** Any program running as your user outside CORSIA can read it, as can anyone who can read your disk while you're signed in. Source: `backend/infra/key_store.py`, `backend/infra/key_store_values.py`.

Values never go into the database or the settings table, so a database backup or a settings read can't carry one. Code decides what uses each key: only fixed readers in the server (provider calls, connected accounts, tool-server launch) resolve a value, and nothing turns a key's name into its value for an agent.

## Who can't read them

| Path to a key | Blocked by | Strength |
|---|---|---|
| An agent's shell reading the key store | The key store is on the hard floor; no lease opens it | Operating system |
| An agent's file tools reading it | Outside the workspace | CORSIA code |
| The shell's environment | Variables named like secrets are removed before a command starts | CORSIA code |
| A command that passes a literal value where a credential goes (a credential-named variable, flag, header or URL field) | Refused before it runs | CORSIA code |
| A leased command that tries to read `.env`, keychain or auth files | The reviewers are told to block it | Model judgment |

## Masking: what a model sees

Every result a CORSIA tool returns passes through a credential masker before it reaches the model (`backend/tools/registry.py`, `backend/host_access/credential_sanitizer.py`). It replaces a value with `[REDACTED]` when it recognizes:

- a value assigned to a name that looks like a credential (`API_KEY=…`, `"password": …`, `token: …`)
- authorization headers and cookies
- user and password in a URL (`https://user:pass@host`)
- PEM private key blocks
- known key shapes: `sk-…`, GitHub `gh?_…` and `github_pat_…`, AWS `AKIA…`/`ASIA…`, Slack `xox…`, Google `AIza…` and `ya29.…`, GitLab `glpat-…`, DigitalOcean `dop_v1_…`

**What masking misses**, each tested for this page:

- A value with no credential-looking name and no known shape, such as a random string on its own line.
- Transformed output. If a command base64-encodes or reverses a file, the result isn't masked.
- Anything a command *does* with the value. Masking changes what the model sees, not what runs. `source .env && curl -H "Authorization: Bearer $TOKEN" …` uses the real value, and the command could send it anywhere.
- An isolated worker on Claude Code reads files with Claude Code's own read tool, which doesn't pass through CORSIA's masker. See [Untrusted content](untrusted-content.md).
- Text you type or paste into chat. Masking applies to tool results; a key you paste goes to the model provider as written.

Masking also has a side effect: an agent that reads a file and writes the whole file back writes the literal text `[REDACTED]` in place of the value, and the value is lost. A targeted edit that names `[REDACTED]` fails instead, because that text isn't in the file.

## A key in a file in your project

If a key sits in a file inside the workspace (a `.env`, a config file):

1. Any agent can read the file. The workspace is the boundary, not the project.
2. What the model sees is masked when the masker recognizes the value (above), so the value usually doesn't reach the model provider.
3. Commands the agent runs can still use the real value, and outbound network access is not limited.
4. A whole-file rewrite by an agent can replace the value with `[REDACTED]`.

**What to do:** keep keys CORSIA uses in **Settings → Keys**, not in workspace files. Keep other secrets outside the workspace. If a file must hold one, assume every agent in the workspace can use it.

## Sign-in tokens for connected apps

Google, Microsoft and tool-server sign-ins store their tokens in the key store. Agents call those services through CORSIA's tools; the token never appears in the agent's context. A tool server you add gets its own keys in its environment when CORSIA starts it; see [Connected apps](connected-apps.md) for how tool servers are confined.

## SSH keys

Private key files in `~/.ssh` are unreadable from agent shells, leased or not. The SSH agent socket is passed through, so keys you have loaded into your SSH agent can be **used** for `ssh` and `git push` without being read. Remove keys from your SSH agent (`ssh-add -D`) if agents shouldn't use them.

## Check it yourself

Each was run from an agent shell on macOS for this page.

1. In a scratch folder in your workspace, create `fake.env` containing `OPENAI_API_KEY=sk-test0000000000000000000000000000` and a line with a random 40-character string on its own.
2. Ask an agent to show the file with `cat fake.env`. The named key shows as `[REDACTED]`; the bare string shows as written.
3. Ask it to run `base64 -i fake.env`. Nothing is masked.
4. Ask it to run `OPENAI_API_KEY=sk-test0000000000000000000000000000 true`. The command is refused before it runs: a literal value where a credential goes. (`echo sk-test0000000000000000000000000000` does run; only its output is masked.)
5. Delete the file.
