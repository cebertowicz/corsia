# Known limits

Every known limit of CORSIA's security, with what it means for you and what to do about it. Each links to the page with the detail. This is the list to read before letting agents work on a machine that matters.

## The ones that matter most

| Limit | What it means | What to do |
|---|---|---|
| **Agents run as you.** The sandbox narrows what a command can touch; it isn't a container, a VM or a separate user ([Agent sandbox](agent-sandbox.md)) | A flaw in the sandbox exposes what your user can reach | For work you can't afford to expose, run CORSIA in a VM or under a separate macOS or Linux user |
| **The workspace is the boundary, not the project** ([Agent sandbox](agent-sandbox.md)) | An agent in one project can read and change every other project, and read their conversations | Keep work that must stay apart in separate installs |
| **Outbound network access is not limited** ([Network](network.md)) | An agent can send what it can read to any website, and reach devices on your local network. `web_fetch` reaches private addresses too | Keep secrets out of the workspace. If you need egress control, add an outbound firewall to the machine |
| **Reviewers are not a defense against a tricked agent** ([Host access](host-access.md)) | The models that review leases default to allow. They assume the applicant is your own agent on a task you gave it, and can't check that you gave it. They are instructed to block a short list (secret exposure, changes to access controls, needless destruction, and commands plainly outside the approved purpose) and can miss those. A tricked agent that asks to read your ordinary personal files with a plausible reason will usually be allowed | Read lease requests before you approve them. Reject any you didn't expect |
| **Under a lease, host commands skip the command refusals** ([Host access](host-access.md)) | For a command the agent marks `host`, `sudo`, `rm -rf ~`, `curl … \| sh` and the rest aren't refused by CORSIA code; only the action reviewer, a model, stands in the way, and your home folder is writable. A literal credential in the command is still refused | Keep leases short and specific |
| **Prompt injection is not solved** ([Untrusted content](untrusted-content.md)) | Email is screened by a local model and labelled, but the text still reaches the agent. Web pages, search results, attachments, files and tool-server results aren't screened. No accuracy is published for the screen | Use an isolated worker for content from strangers. Don't connect accounts an agent doesn't need |
| **No outside security audit** | Nobody outside the project has tested these controls | Weigh that before trusting them with anything sensitive |

## Secrets

| Limit | What it means | What to do |
|---|---|---|
| Stored keys aren't encrypted ([Secrets](secrets.md)) | The key store is a file only your user can read, outside every agent's reach, but any other program running as you can read it | Use disk encryption; don't run untrusted programs as the same user |
| Masking misses values with no recognizable name or shape, and transformed output ([Secrets](secrets.md)) | Some credentials in workspace files can reach the model provider | Keep keys in **Settings → Keys**, not in workspace files |
| Masking doesn't stop commands from using a value ([Secrets](secrets.md)) | `source .env && curl …` uses and can send the real key | Same |
| An isolated worker on Claude Code reads files without masking ([Untrusted content](untrusted-content.md)) | Credentials in the folders it reads reach the model unmasked | Don't point isolated workers at folders holding keys |
| Your loaded SSH keys and GitHub CLI login are usable from agent shells ([Agent sandbox](agent-sandbox.md)) | Agents can `git push` and call GitHub as you | Remove keys from your SSH agent, or set up a separate agents' GitHub account. Agent shells can read that account's token, so give it access only to the repositories agents need |

## Sending and connected apps

| Limit | What it means | What to do |
|---|---|---|
| The send gate checks that you replied after the draft, not that you said "send" ([Connected apps](connected-apps.md#sending-email-and-invites)) | Any reply, even a question, lets the draft go | Reply only when you're ready, or say "don't send" plainly |
| Only CORSIA's own email and invite tools are gated ([Connected apps](connected-apps.md#sending-email-and-invites)) | Tool servers, the agents' browser, shell commands and OneDrive uploads send without the gate | Connect only tool servers you need; don't sign the agents' browser in to your mail |
| Local tool servers run with wide access to your home folder ([Connected apps](connected-apps.md#tool-servers)) | A tool server you add can read Documents, `~/.ssh` and other places agents can't | Add only tool servers you trust like any installed program |
| Connected-account sign-ins grant more than CORSIA's tools use ([Connected apps](connected-apps.md)) | A Google sign-in allows reading, sending and trashing all mail and changing all calendars and files | Connect only the accounts agents need |
| The agents' browser profile is shared by all your agents ([Connected apps](connected-apps.md#the-agents-browser)) | A site you sign in to there is usable by every agent of yours | Sign out of anything agents shouldn't use |

## Accounts and network exposure

| Limit | What it means | What to do |
|---|---|---|
| Sign-in is password only, slowed but never locked out ([Accounts and access](accounts-and-access.md)) | A server setup reachable from other networks can be guessed against | Use the local setup, or a private tailnet; never expose CORSIA to a network you don't control |
| Installs with no machine setup file listen on every interface ([Network](network.md)) | An install started with `CORSIA_WORKSPACE` set and no setup file may be reachable from your network, and `corsia setup local` refuses there | Unset `CORSIA_WORKSPACE` and run `~/.corsia/scripts/corsia`, which runs first-time setup and asks for the setup choice |
| API tokens never expire ([Accounts and access](accounts-and-access.md)) | A leaked token works until revoked | `corsia token list`, then `corsia token revoke` any you don't use |
| The web app keeps its session token in browser local storage ([Accounts and access](accounts-and-access.md)) | A script-injection flaw in CORSIA's page would expose it | Sign out on shared computers |
| Accounts on one install aren't separated ([Accounts and access](accounts-and-access.md)) | Agents of one account can read another's files and conversations | One install per person |
| During first-run setup, before an account exists, any program on the machine can use the setup routes ([Accounts and access](accounts-and-access.md)) | A local program could create the first account | Create the account right after installing |
| A few routes take no sign-in from programs on this machine, agent shells included ([Network](network.md#what-listens)) | Any of them can stop the local model servers CORSIA manages, interrupting chats running on a local model | — |

## Supply chain and updates

| Limit | What it means | What to do |
|---|---|---|
| The Python lock pins versions, not hashes ([Supply chain](supply-chain.md)) | A replaced package file on the index wouldn't be caught | — |
| Updates check no signatures ([Supply chain](supply-chain.md)) | Whoever can push to the branch you track decides what your install runs next | Leave automatic updates off and update when you choose; read what changed first |
| Signing the desktop app is a manual step outside the release workflow ([Supply chain](supply-chain.md#the-desktop-app-and-releases)) | The repository doesn't show whether a download was signed and notarized | Check a download with `spctl` before opening it, or build from source |
| With the skill library on, agents install skills without asking ([Supply chain](supply-chain.md#shared-skills-and-setups)) | A library skill can instruct an agent to do anything that agent can already do | Leave community improvements off if you don't want that |
| Vulnerability checks are manual ([Supply chain](supply-chain.md)) | Known-vulnerable dependencies aren't flagged automatically | Run `scripts/security-audit.sh` |

## Audit

| Limit | What it means | What to do |
|---|---|---|
| Audit records aren't tamper-evident ([Audit](audit.md)) | A leased command or any program running as you can edit them | Copy records off the machine if you need them to hold |
| Network traffic isn't recorded ([Audit](audit.md)) | You can't see after the fact what an agent sent, beyond its tool calls | — |
| No screen in the app shows audit history ([Audit](audit.md)) | Reading it needs `sqlite3` or an agent's summary | — |

## Linux

| Limit | What it means |
|---|---|
| Leased commands and local tool servers need Agent Safehouse, which is macOS only ([Host access](host-access.md), [Connected apps](connected-apps.md)) | Both are expected to refuse on Linux |
| The sandbox has no network namespace ([Agent sandbox](agent-sandbox.md)) | Network access is the same as on macOS |
| The Linux behavior in these pages was read from code and tests, not run | Treat it as less verified than the macOS behavior |

## Out of scope

Some things aren't defended against at all, such as malware running as your user or physical access to an unlocked machine. The list, and what CORSIA assumes, is in [Threat model](threat-model.md#out-of-scope).
