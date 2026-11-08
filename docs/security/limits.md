# Known limits

Every known limit of CORSIA's security, with what it means for you and what to do about it. Each links to the page with the detail. This is the list to read before letting agents work on a machine that matters.

## The ones that matter most

| Limit | What it means | What to do |
|---|---|---|
| **Agents run as you.** The sandbox narrows what a command can touch; it isn't a container, a VM or a separate user ([Agent sandbox](agent-sandbox.md)) | A flaw in the sandbox exposes what your user can reach | For work you can't afford to expose, run CORSIA in a VM or under a separate macOS or Linux user |
| **Projects are only partly separated** ([Agent sandbox](agent-sandbox.md#projects)) | A project agent's shell and file tools can't reach other projects without a lease, and the CORSIA agent is fenced less ([Agent sandbox](agent-sandbox.md#projects)). Its chat tools can still read every project's chats, including those of projects set to Local models only | Keep work that must stay apart in separate installs |
| **Outbound network access is not limited** ([Network](network.md)) | An agent can send what it can read to any website, and reach devices on your local network. `web_fetch` reaches private addresses too | Keep secrets out of the workspace. If you need egress control, add an outbound firewall to the machine |
| **Reviewers are not a defense against a tricked agent** ([Host access](host-access.md)) | The models that review leases default to allow. They assume the applicant is your own agent on a task you gave it, and can't check that you gave it. They are instructed to block a short list (secret exposure, changes to access controls, needless destruction, and commands plainly outside the approved purpose) and can miss those. A tricked agent that asks to read your ordinary personal files with a plausible reason will usually be allowed | Read lease requests before you approve them. Reject any you didn't expect |
| **Under a lease, host commands skip the command refusals** ([Host access](host-access.md)) | For a command the agent marks `host`, `sudo`, `rm -rf ~`, `curl … \| sh` and the rest aren't refused by CORSIA code; only the action reviewer, a model, stands in the way, and your home folder is writable. A literal credential in the command is still refused | Keep leases short and specific |
| **The hard floor binds one command, not what it leaves behind** ([Host access](host-access.md#what-no-lease-opens)) | A leased command can't open the key store, but it can change what runs later outside the sandbox, such as your shell startup files, startup items or CORSIA's own code, and that code can read stored keys. Only the reviewers stand in the way | Treat a leased command as able to reach everything your user can. Keep Auto-approve off on a machine where that matters |
| **Prompt injection is not solved** ([Untrusted content](untrusted-content.md)) | Email is screened by a local model and labelled, but the text still reaches the agent. Web pages, search results, attachments, text messages, files, Google documents and connected app results aren't screened. No accuracy is published for the screen | Use an isolated worker for content from strangers. Don't connect accounts an agent doesn't need |
| **No outside security audit** | Nobody outside the project has tested these controls | Weigh that before trusting them with anything sensitive |

## Secrets

| Limit | What it means | What to do |
|---|---|---|
| Stored keys aren't encrypted ([Secrets](secrets.md)) | The key store is a file only your user can read, and no agent command can open it directly, but any other program running as you can read it | Use disk encryption; don't run untrusted programs as the same user |
| Masking misses values with no recognizable name or shape, placeholder words, some one-word values, and transformed output ([Secrets](secrets.md#masking-what-a-model-sees)) | Some credentials in workspace files can reach the model provider | Keep keys in **Settings → Keys**, not in workspace files |
| Masking doesn't stop commands from using a value ([Secrets](secrets.md)) | `source .env && curl …` uses and can send the real key | Same |
| An isolated worker on Claude Code reads files without masking ([Untrusted content](untrusted-content.md)) | Credentials in the folders it reads reach the model unmasked | Don't point isolated workers at folders holding keys |
| Your loaded SSH keys and GitHub CLI login are usable from agent shells ([Agent sandbox](agent-sandbox.md)) | Without an agents' GitHub account, agents can `git push` and call GitHub as you. With one, GitHub pushes and `gh` run as that account, but a command that sets its own SSH command can still use your key | Remove keys from your SSH agent, or set up a separate agents' GitHub account. Agent shells can read that account's token, so give it access only to the repositories agents need |
| Claude Code and Codex sign-ins sit in CORSIA's state folder ([Secrets](secrets.md#where-keys-live)) | Agent shells and leased commands cannot directly read them; other programs running as your user can | Don't run untrusted programs as the same user |

## Sending and connected apps

| Limit | What it means | What to do |
|---|---|---|
| The send gate checks that you replied after the draft, not that you said "send" ([Connected apps](connected-apps.md#sending-email-and-invites)) | Any reply, even a question, lets the draft go | Reply only when you're ready, or say "don't send" plainly |
| Only CORSIA's own email, invite and text-message tools are gated ([Connected apps](connected-apps.md#sending-email-and-invites)) | Connected apps, the shared browser, shell commands, OneDrive uploads, and Google file shares and calendar changes that email guests (the `google_workspace` tool) send without the gate | Connect only connected apps you need; don't sign the shared browser in to your mail |
| Local connected apps run with wide access to your home folder ([Connected apps](connected-apps.md#local-and-remote-connected-apps)) | A connected app you add can read Documents, `~/.ssh` and other places agents can't | Add only connected apps you trust like any installed program |
| Connected-account sign-ins grant more than CORSIA's tools use ([Connected apps](connected-apps.md)) | A Google sign-in allows reading, sending and trashing all mail and changing all calendars and files | Connect only the accounts agents need |
| The shared browser profile is shared by all your agents ([Connected apps](connected-apps.md#the-shared-browser)) | A site you sign in to there is usable by every agent of yours | Sign out of anything agents shouldn't use |
| Messages needs Full Disk Access for CORSIA ([Connected apps](connected-apps.md#messages)) | Once granted, leased commands and local connected apps can also read every file macOS protects, such as Mail, Messages and Photos | Grant it only if you need agents to use Messages; switch CORSIA off under Full Disk Access to undo it |

## Accounts and network exposure

| Limit | What it means | What to do |
|---|---|---|
| Sign-in is password only, slowed but never locked out ([Accounts and access](accounts-and-access.md)) | A server setup reachable from other networks can be guessed against | Use the local setup, or a private tailnet; never expose CORSIA to a network you don't control |
| Installs with no machine setup file listen on every interface ([Network](network.md)) | An install started with `CORSIA_WORKSPACE` set and no setup file may be reachable from your network, and `corsia setup local` refuses there | Unset `CORSIA_WORKSPACE` and run `~/.corsia/scripts/corsia`, which runs first-time setup and asks for the setup choice |
| The CORSIA agent and project conductors can call almost every CORSIA API route ([Accounts and access](accounts-and-access.md#how-agents-act)) | A tricked conductor reaches well beyond an ordinary agent's tools, and across projects; only a list of person-only routes is closed to it | Don't point a conductor at content you don't trust |
| API tokens never expire ([Accounts and access](accounts-and-access.md)) | A leaked token works until revoked | `corsia token list`, then `corsia token revoke` any you don't use |
| The web app keeps its session token in browser local storage ([Accounts and access](accounts-and-access.md)) | A script-injection flaw in CORSIA's page would expose it | Sign out on shared computers |
| Accounts on one install aren't separated ([Accounts and access](accounts-and-access.md)) | Agents of one account can read another's files and chats | One install per person |
| During first-run setup, before an account exists, any program on the machine can use the setup routes ([Accounts and access](accounts-and-access.md)) | A local program could create the first account | Create the account right after installing |
| A few routes take no sign-in from programs on this machine, agent shells included ([Network](network.md#what-listens)) | Any of them can stop the local model servers CORSIA manages, interrupting chats running on a local model | — |

## Supply chain and updates

| Limit | What it means | What to do |
|---|---|---|
| The Python lock pins versions, not hashes ([Supply chain](supply-chain.md)) | A replaced package file on the index wouldn't be caught | — |
| Source-install updates check no signatures ([Supply chain](supply-chain.md#updates)) | Whoever can push to the branch you track decides what a source install runs next | Leave automatic updates off and update when you choose; read what changed first |
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
| Leased commands, local connected apps and Google Workspace need Agent Safehouse, which is macOS only ([Host access](host-access.md), [Connected apps](connected-apps.md)) | All three are expected to refuse on Linux |
| A project folder or repository that doesn't exist yet isn't hidden ([Protected commands on Linux](../linux-sandbox.md)) | An agent in another project can create it and write into it; once it exists, the next command hides it |
| The sandbox has no network namespace ([Agent sandbox](agent-sandbox.md)) | Network access is the same as on macOS |
| The Linux behavior in these pages was read from code and tests, not run | Treat it as less verified than the macOS behavior |

## Out of scope

Some things aren't defended against at all, such as malware running as your user or physical access to an unlocked machine. The list, and what CORSIA assumes, is in [Threat model](threat-model.md#out-of-scope).
