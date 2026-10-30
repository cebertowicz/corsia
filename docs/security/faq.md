# Security FAQ

Short answers to the hard questions. Each links to the page with the detail and the source.

## Blast radius

**1. If an agent goes wrong, what's the worst it can do to my machine?**
Without a lease: read and change everything in the workspace, every project included; read a few tool-settings folders; use your loaded SSH keys and GitHub CLI login; and send anything it can read to any address on the internet or your local network. It can't read the rest of your home folder, or write outside the workspace apart from temporary folders, `~/.ssh/known_hosts` and some developer-tool caches. With a lease, a command can reach most of your home folder, checked only by a reviewer model. See [Agent sandbox](agent-sandbox.md) and [Host access](host-access.md).

**2. Can an agent delete my home folder? My git history? My other projects?**
- Home folder: not without a lease. Writes to your home folder are refused by the OS, apart from `~/.ssh/known_hosts` and some developer-tool caches ([Agent sandbox](agent-sandbox.md#whats-allowed)), and `rm -rf ~` is refused by CORSIA code. Under a lease, the action reviewer, a model, is told to block needless destruction; nothing else stops it.
- Other projects: yes. Every project in the workspace is writable.
- Git history: local history of repositories in the workspace, yes, except the main checkout of a registered repository whose base is protected, which is read-only. Remote history: an agent using your SSH keys or GitHub login can force-push wherever you can, unless the remote protects the branch.

**3. Can an agent in one project read another project? Why is that allowed?**
Yes. Projects organize work, settings and what each agent is given; they aren't designed to keep agents apart, and the code says so (`backend/tools/filesystem_gate.py`: project scope is not a confidentiality boundary). The OS sandbox works at the workspace folder. Keep work that must stay apart in separate installs. See [Accounts and access](accounts-and-access.md#what-a-project-does-and-doesnt-separate).

**4. Agents run as my user. Isn't that the same as no sandbox?**
No. The OS sandbox refuses reads of most of your home folder and writes outside the workspace, apart from a few named exceptions such as temporary folders, whatever the command. But it isn't a separate user or a container: a flaw in the sandbox exposes what your user can reach, and anything agents can read they can send out. See [Agent sandbox](agent-sandbox.md).

**5. What happens if the sandbox fails to start, or isn't installed?**
Shell commands don't run. On macOS a missing or too-old Agent Safehouse gives "Nothing ran"; on Linux the shell refuses without bubblewrap. File tools keep working inside the workspace, because their check is CORSIA code. See [Agent sandbox](agent-sandbox.md).

**6. Why not a container or VM? When should I use one anyway?**
CORSIA has no container or VM mode: agents work on your real files with your own tools, and the sandbox narrows that rather than replacing it. Run CORSIA inside a VM or under a separate user account yourself when the machine holds data you can't afford to expose, or when agents will handle content from strangers.

**7. Can an agent escape the sandbox? What's your evidence? Has it been tested by anyone outside the project?**
We know of no escape. The evidence is the repository's tests and the probes each page lists under "Check it yourself", run for these pages on macOS. Nobody outside the project has tested it. The Linux sandbox was read from code and tests, not run for these pages.

**8. What are the Safehouse and bubblewrap trust roots, and who maintains them?**
On macOS: [Agent Safehouse](https://github.com/eugene1g/agent-safehouse), an independent open-source project, which generates profiles for Apple's `sandbox-exec`. Apple marks `sandbox-exec` deprecated, but the kernel still enforces it. On Linux: [bubblewrap](https://github.com/containers/bubblewrap), maintained by the containers project, over Linux namespaces. CORSIA trusts both and the OS kernel beneath them.

## Secrets

**9. Can an agent read my API keys, SSH keys, cookies, Keychain, `.env` files?**
- Keys CORSIA stores: no, not even with a lease.
- SSH private keys, Keychains, your browsers' cookies: not without a lease, and a lease reviewer is told to block reading keychain and auth files. It can **use** keys loaded in your SSH agent.
- The agents' own browser cookies: no, not even with a lease.
- `.env` files: yes, if they are in the workspace. Values the masker recognizes are hidden from the model, but commands can still use them.

See [Secrets](secrets.md).

**10. A key is in a file in my project. What happens?**
Any agent can read the file. The model usually sees `[REDACTED]` in place of a value the masker recognizes, but commands can use the real value and send it anywhere. Move keys CORSIA uses into **Settings → Keys**. See [Secrets](secrets.md#a-key-in-a-file-in-your-project).

**11. Can a model see a credential at all? What does masking cover and miss?**
Yes, sometimes. Masking covers credential-named values, authorization headers, URL passwords, private key blocks and common key shapes. It misses values with no telling name or shape, transformed output, what you paste into chat, and files read by an isolated worker on Claude Code. See [Secrets](secrets.md#masking-what-a-model-sees).

**12. Where are provider keys stored, and are they encrypted?**
In one file in CORSIA's state folder, readable only by your user and closed to every agent route. It is not encrypted. See [Secrets](secrets.md).

## Data

**13. What leaves my machine, exactly? What goes to the model provider?**
On a cloud model, each turn sends the instructions, the conversation and every tool result, including the contents of files and pages the agent read. Other traffic goes to services you connect, websites agents visit, and a few anonymous downloads from GitHub. See [Data and privacy](data-and-privacy.md#what-leaves-your-machine).

**14. Does Local only mean nothing leaves?**
No. Local only means a chat may run only on models served from this machine, or from a server you marked as a trusted device. Its agent can still use the web, email, tool servers, GitHub and the network. See [Data and privacy](data-and-privacy.md#local-only).

**15. Do you collect telemetry? Can I turn it off? Does anything phone home?**
No product analytics or crash reports are sent. CORSIA does make anonymous requests to GitHub for update checks, a model price list and the skill library index. See [Data and privacy](data-and-privacy.md#what-leaves-your-machine) for each and how to stop it.

**16. Is my data encrypted at rest?**
Not by CORSIA. Use FileVault or Linux disk encryption. See [Data and privacy](data-and-privacy.md#whats-stored-and-where).

**17. How do I remove everything?**
Stop CORSIA, delete the state folder, the workspace and the checkout or app, and revoke its access in your Google or Microsoft account. See [Data and privacy](data-and-privacy.md#removing-everything).

## Prompt injection

**18. A web page or email tells my agent to upload my files. What stops it?**
Not much, if the files are in the workspace. Web pages aren't screened; email is screened and labelled, but the agent still sees the text. Outbound network access isn't limited, so an agent that follows the instruction can upload anything it can read. Files outside the workspace need a lease, but **the reviewers won't reliably stop this**: they default to allow, assume the applicant is your own agent on a task you gave it, and can't check that you gave it. They are instructed to block a short list: secret exposure, changes to access controls, needless destruction, and commands plainly outside the approved purpose. A tricked agent that asks to read your ordinary personal files with a plausible reason will usually be allowed. What remains is you reading each lease request. See [Untrusted content](untrusted-content.md) and [Host access](host-access.md).

**19. Can an agent send data to any website?**
Yes. Outbound access is not limited. See [Network](network.md#outbound).

**20. How good is the email screen? What's the false negative rate?**
Unknown. No rate is published, and it depends on your local model. You can measure it on your own model with the synthetic set in the repository. See [Untrusted content](untrusted-content.md#what-is-screened).

**21. Can I trust a skill from the library? What does your review actually check?**
Every file is checked against the hash in the library's index, so you get what the library currently publishes. The library's own items follow its `main` branch, so whoever can push there decides what the next install gets; items from other repositories are pinned to a commit. No review checklist is published, and outside contributions aren't open yet. A skill can tell an agent to do anything that agent can already do. The library is off until you agree to use community improvements. See [Supply chain](supply-chain.md#shared-skills-and-setups).

## The reviewer

**22. Isn't a model reviewing a model circular?**
Partly, yes. The reviewer is a separate call that sees only the request, not the conversation that may have misled the agent. That keeps the conversation out, but not injected text: an agent that was misled can repeat that text in its application or commands. The reviewer is told to treat their content as quoted data, not instructions. And it is a model, by default the agent's own, and it is told to let the work proceed: it defaults to allow, assumes the applicant is your own agent on your task, and can't check that you gave that task. It is instructed to block a short list: secret exposure, changes to access controls, needless destruction, and commands plainly outside the approved purpose. It is **not** a defense against a tricked agent asking for your ordinary personal files. The hard floor and the sandbox are CORSIA code and the OS; the review is not. See [Host access](host-access.md#what-the-reviewers-are-told-to-block-and-what-they-arent).

**23. What happens when the reviewer is down or wrong? How often does it block good work or pass bad work?**
Down: an application is blocked, and a command under an active lease is denied. Wrong: nothing catches it except you. No measurement of wrong approvals or wrong blocks is published; the README reports how often it said yes and no in one week. See [Host access](host-access.md#when-something-fails).

**24. Can an agent approve its own access, or talk the reviewer into it?**
It can't approve: approval needs a signed-in person session, or the reviewer's own allow when **Auto-approve** is on, a setting only a person can change. It can try to talk the reviewer into it, and the reviewer is built to say yes to a plausible purpose: it defaults to allow, assumes the agent is yours and on your task, and can't verify your instruction. It is instructed to block a short list (secret exposure, changes to access controls, needless destruction, and commands plainly outside the approved purpose) and can miss those; so it is **not** a defense against a tricked agent asking for ordinary personal files with a good story. See [Host access](host-access.md).

**25. What does automatic approval change, and what's the safe default?**
The setting is **Auto-approve**, under **Settings → System → Host access review**. With it on, an application the reviewer allows becomes active at once; the model's verdict is the only approval. With it off, every allowed application waits for you. Off is the safe setting. See [Host access](host-access.md#approval-modes).

## Exposure

**26. Is it safe to run on a server, on a shared machine, or on a network I don't control?**
- A server you alone use, reached over a private tailnet: reasonable.
- A shared machine: no. Anyone signed in to your user account can read everything CORSIA stores, and accounts on one install aren't separated.
- Listening on a network you don't control: no. Sign-in is password only, with no second factor.

See [Network](network.md) and [Accounts and access](accounts-and-access.md).

**27. Can someone on my network reach it? What about my phone?**
With the local setup, no: it listens only on this machine. A server setup, or an old install with no setup recorded, listens on every interface behind sign-in. Phones connect through a private tailnet, or through Devices pairing, which you turn on. See [Network](network.md).

**28. Is this multi-user? Can two people share an install?**
No. More than one account can exist, but their agents run as the same user in the same workspace and can read each other's files and conversations. One install per person. See [Accounts and access](accounts-and-access.md#more-than-one-account).

**29. How do I report a vulnerability?**
Read [`SECURITY.md`](../../SECURITY.md) first.

**30. Has this had a security audit?**
No. Nobody outside the project has audited or tested it.

## Compliance

**31. Is it suitable for regulated data (HIPAA, SOC 2)?**
CORSIA makes no compliance claim: no HIPAA, SOC 2 or other certification, attestation or agreement. It sends conversation content to the model providers you choose, stores data unencrypted, and keeps an audit trail that isn't tamper-evident. Whether you may put regulated data through it depends on your obligations and your providers' terms; it isn't designed for it.
