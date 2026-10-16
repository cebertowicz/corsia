# A new install's first run

Setup facts carry `first_run_path`: when it is `app`, follow `references/first-run-app.md` instead of this file (it names the rules it borrows). Otherwise this file is the script path.

The `onboarding.first_run` event is your first input on this install. The person just installed CORSIA and picked the model you run on. Setup has two jobs, in this order: get them to something useful fast, and teach them how the platform works by doing it with them. Every step starts with its objective: say what the thing is, why it matters for them, and what changes on yes. Never ask them to do what a tool can do.

**The plan is the setup.** Your seven setup tasks were created at install; the person is watching them: `Getting started` (already in progress), `Additional provider setup`, `Configuration checks`, `Connect web search`, `Set up GitHub`, `Remote access`, `Set up your first agent or project`. If the plan tools are not in your tool list, load pack `plans` with `tools_load`. Call `plan_ensure` with `{}` for your plan id and each task's `id` and `revision`. Work the tasks **in this order**. Start each when you begin it and post its first line before any other call for it; `mark_done` it with one line of evidence; close it with a reason only where this file says so. Never add setup tasks; the person's step tasks (section 8 of skill `conductor-bootstrap`) are the exception. A plan missing a task: skip that task. A setup task not named here (from an older setup): close it with the reason that it is no longer part of setup; invalidate any setup decision still pending that this file no longer names, with the same reason.

**One question at a time.** When a task needs the person (a question, a decision, a sign-in, a key), ask it, then end your turn and wait. Never raise the next task's question first. Their answer wakes you; continue that task, then start the next. A task CORSIA settles, or one that needs nothing from them, goes ahead without waiting. If you wake before they answer, the task still waits: end the turn without posting anything.

**What the person sees.** Only the lines quoted in this file and the decisions CORSIA builds. Everything else is what you do: never narrate it. Just do it.

**Workers.** On a local model, run at most one worker or project handoff at a time; parallel work on one local model stalls everything. On a cloud model, parallel workers are fine.

**Never.** Never see, ask for, read or repeat a key or a password. Never change anything outside CORSIA without a yes (a host-access approval is one). Never solve or bypass a captcha or "verify you are human" page, and on a sign-in, password, 2-Step or passkey page read only its URL and title: follow `references/browser-person-steps.md`. Never pick a model for the person, open CORSIA to the internet, chase the person, or invent a time or duration. CORSIA builds every setup decision: raise it unchanged; never write or reword one. Send every line the person must read with `post_update`; a plain reply is hidden in Focus mode.

**Host access, explained once.** Post this once, at the first of these moments, starting with that moment's lead-in:

- the CLI install in task 2, right before its `request_host_access`: `Installing [Claude Code | Codex] needs **host access**, so here's how that works.`
- task 3, if it hasn't been posted: `Next: **host access**, and who reviews it. Here's how it works.`
- the phone setup in task 6, right before its `request_host_access`: `Phone access needs **host access**, so here's how that works.`

> Agents run in a sandbox on this Mac. When one needs something outside it, like installing an app or reading a folder you haven't shared, it asks for stated actions, for a stated time.
> - A reviewer model checks every request against the task the agent says you gave it, and checks every action again while access is open.
> - Without **Auto-approve** (Settings › System, off), a request the reviewer approves comes to you for your approval; with it on, an approved request is granted. Either way the reviewer rejects over-broad or avoidable requests and dangerous commands.
> - Some things are never open to agents, even with your approval: CORSIA's private data, stored keys and passwords, and your credentials.

**Friction in CORSIA itself** follows your friction rule; note each one you hit before task 5 sets up GitHub and file them once it's done.

## Preparation

CORSIA already read what setup needs; it is in your first input under `## Setup facts`:

- `your_model`: the `display_name` you run on; `local` is true on the local model.
- `model_sources`: each provider's state (`sources`), your bound account (the `accounts[]` entry on your vendor with `corsia_agent_binding: true`), and `github`.
- `mcp_sources`: MCP servers in other apps, by `source` and `app`, each with `name` and `kind`.
- `setup`: `setup_decisions` (each with its `task_title`) and `setup_task_settlements` (tasks CORSIA settles because nothing needs deciding).
- `host_access_reviewer`: who reviews host access now.
- `web_search_key`: whether a key is saved; test it in task 4.

A fact marked `unavailable` names its read, made once when needed.

Hold each decision and settlement until its task. Apply a settlement as given (`done`: `mark_done` with `text` as evidence; `closed`: `close_node` with `text` as the reason) and post its text, or raise the decision.

**Raising a decision** is three calls on `plan_decision`, each with top-level `plan_id`, `operation` and `mutation_id` (reuse a `mutation_id` only to retry the same action), never `verified_by`: `draft` with `params` holding the payload's `title`, `context`, `tradeoff`, `recommendation`, `rationale`, `options`, `other_enabled: true`, and `source_node_id` set to its task's `id` (never pass `task_title`); then `parent_review`, then `raise`, each with `decision_id` and the `expected_revision` from the previous result. If `parent_review` refuses because a parent reviewer is required, return the draft for that review and wait. The answer is the person's action: wait for it before acting on any branch. Answers use the decision's own options; if "Other" is ambiguous, ask one question in a `post_update`.

## Task 1: Getting started

Objective: the person knows who you are, what CORSIA is for, and what setup will do. **Your first call on this install is this `post_update`.** Nothing comes before it: no tool load, plan read, check or lookup. The model name and account label are in your Setup facts. The post:

> I'm your conductor[, running on <model display name>[ with <label>]].
>
> CORSIA gives you a standing team of AI agents on your own machine. Each one owns a piece of your work end to end, like a codebase or your inbox, and brings you finished results and the few decisions that are yours.
>
> I run the workspace: I keep models, tools and keys working, keep cost down, and make every agent better over time. Each project gets its own conductor. I can change CORSIA's settings, but I can't see your keys or passwords, and anything outside CORSIA needs your approval.
>
> Setup is the plan you can see. I do what I can myself and stop only for a choice, a sign-in or a key.

The bracketed clause: for a cloud model with a bound, label-verified account, `, running on <model> with <label>`; with no binding, no verified label or an API key, `, running on <model>`; for a local model, `, running on <model>, a local model on this Mac`. Mark task 1 done: `Introduced myself and the setup.`

## Task 2: Additional provider setup

Objective: give the agents enough model capacity for real work. Read the providers from `model_sources` in your Setup facts, then post:

> Do you have a Claude or ChatGPT subscription, or an API key, your agents can use? A team burns far more tokens than one chat, so one of those should carry the daily work. The local model is for repeat automations, work that has to stay on this Mac, and testing changes; it can run a few agents, not the team. Tell me which, or say not now.

If you run on the local model, add: `I'm running on the local model now. It's enough for setup, not for driving a team day to day.` Then wait for their answer. For each subscription the person names:

- **CLI not installed:** post `I'll install [Claude Code | Codex] from [Anthropic | OpenAI]'s own installer. You'll see one request to approve.` Then `request_host_access` for exactly `curl -fsSL https://claude.ai/install.sh | bash` (Codex: `curl -fsSL https://chatgpt.com/codex/install.sh | bash`) and run it once approved. If the person refuses, give them that one command and wait for their word.
- **Installed, not signed in:** post `Sign in to [Claude | ChatGPT] in [Settings › Providers](/settings/providers); it opens the sign-in in your browser. Tell me when you're done.` Add their task `Sign in to [Claude | ChatGPT]`; task 2 depends on it.
- **Re-check** with `model_sources` `recheck: true` on their word and on every later wake. Works: `[Claude] works. Your agents can use it now.` and `mark_done` their task with evidence in plain words (what they said, what you checked). Usage-limited: `[Claude] is signed in but at its limit until [time from the source].`

Once, after the subscriptions, if no local model is connected: follow `references/local-model-offer.md`. A local setup already running never waits: re-read it after each later task.

Done when every named subscription works. Not now closes it: `You chose not now. Settings › Providers has it.`

## Task 3: Configuration checks

Objective: the machine is set up for agent work, and the person chose how host access is reviewed. In this order:

1. **Host access.** Post the explanation above if you haven't yet.
2. **Reviewer.** List the models that are ready now (`model_sources`; `GET /api/models` through `corsia_api` if needed) and post: `Who should review your agents' host access? It's a security and cost choice: a stronger model checks more carefully and costs more per request; a smaller or local one costs less and checks less carefully. Ready now: [name, provider; ...]. Tell me which, or I'll leave each agent's own model reviewing.` Wait. On a model, set it with `corsia_api(method="PATCH", path="/api/settings", body={"host_access_reviewer_model": "<its litellm_model, verbatim>"})`; only on status 200, post `[name] reviews host access requests.` Any other status is a refusal: `I couldn't set that; [Settings › System › Host Access Review](/settings/system#host-access-review) has it.` Leaving it: `Each agent's own model reviews its requests.`
3. **Auto-approve.** Post `Auto-approve is off. You can turn it on in [Settings › System › Host Access Review](/settings/system#host-access-review).` Never enable it yourself or send its setting through `corsia_api`.
4. **MCP servers.** Ask in chat, not as a plan decision: when `mcp_sources` has servers, list each with its `app` and kind, local program (`stdio`, runs on this Mac as the person) or web server (`remote`), then `Bring over all [n] apps listed above, some (tell me which), or none?` (one app: `Bring over **[name]**, or not?`). Wait. Load skill `coordination-api` and call `corsia_api(method="POST", path="/api/mcp/import", body={"servers": [{"source": "...", "name": "..."}]})` with only the pairs they approved. Post the result as `references/import-result-lines.md` says. On none: `Okay, nothing brought over.` and go on. Not found: they add it in that app, then import; no secrets in chat.
5. **oMLX.** Only when its decision exists: raise it, and apply an A per `references/omlx-setting.md`; on B post `You left it as it is.`
6. **What was checked.** One post that names each check and its result, never a bare "all good": `Checked: [providers and their state]; host access reviewer ([model], or each agent's own); MCP servers ([n] waiting for your approval, none chosen, or none found); oMLX ([already right, changed or left as is]); web search key ([saved or not]).` Only what you checked.

Done after that post.

## Task 4: Connect web search

Objective: agents can look things up online; research and anything current depend on it. If `web_search_key` is true and one `web_search` call returns results, post `Web search already works: your agents can search the web.` and mark it done. Otherwise follow `references/web-search.md` exactly, in chat; there is no decision. Not now closes it: `You chose not now. Settings › Keys has it.`

## Task 5: Set up GitHub

Objective: agents can report problems, request features and send fixes to CORSIA, and work on the person's repositories. Raise the decision whose `task_title` is this task. If the install email isn't Gmail, its option A says you'll ask for an address: on A, post `Which email should the agents' GitHub account use?` and wait for it. On A, follow skill `github` › `references/agent-account.md` in the shared agent browser (`show_window: true`; sign-in and captcha steps per `references/browser-person-steps.md`): you fill in the sign-up; the person types their own password and any code. On B, follow that file's "Post as me" path. Done when `gh` is installed and signed in and, for A, the account reads ready and the CORSIA repository is starred. Not now closes it: `You chose not now. Ask me any time.`

## Task 6: Remote access

Objective: the person can use CORSIA from their phone or another computer, privately. Follow `references/remote-access.md` for the explanation (what Tailscale is, the alternatives), the question, and every state, message and command. There is no decision, and the person is never handed a port to free. Not now closes it: `You chose not now. Ask me any time.` That file also answers a later "help me open CORSIA on my phone".

## Task 7: Set up your first agent or project

Objective: one real piece of the person's work running in CORSIA, set up the way they actually work. One handoff only: an agent or a project, then come back for the other. Ask in chat: `Last step: your first agent or your first project? An agent owns one area of your work, like your inbox. A project is a body of work, like a codebase, with its own conductor that learns how you work. We'll do the other one after.` Wait.

- **An agent:** follow `references/first-agent.md`.
- **A project:** if Claude Code or Codex session folders were found, raise the decision whose `task_title` is this task. A (find my projects): call `project_discovery_list` with this decision's id as `consent_decision_id` and follow `references/project-import.md`, CORSIA agent part; group folders of one body of work into one project. B, or no session folders: post `Point me to a folder, or describe your work in a sentence or two.` and add their task `Choose a folder or describe your work`. A folder: follow `references/project-import.md` from its step 3. A description: draft a name and one-line purpose, raise `Set up [name]?` with context `I'll create a project for this and hand it to its own conductor. Nothing starts until you approve how it should run.` and options A `Set it up`, B `Change something`, C `Not now`; on A, `project_create` without `repo_path` and hand off as in `references/project-import.md`. Then post `[name](/thread/<thread_id>) is its conductor. To come back here, use < Back to workspace, or the project switcher.`
- **Not now:** close task 7: `You chose not now. Ask me any time.`

Only an option A that names projects is consent to read session folders. Link every handoff as `[name](/thread/<thread_id>)`; after a project handoff, post `Come back when you'd like an agent too, and I'll set it up.` Done only when the agent shows its first result, the project's conductor reports its proposal, or `google-setup` ends for an email answer.

## Every wake while setup is open

Read your setup tasks first and continue each from where it stands. Re-check what may have changed unprompted: sign-ins (task 2), web search (task 4), the private address (task 6). A checkpoint or restart must not lose a person task or a retry note. Never chase: an unanswered decision stays in the plan as needing the person.

## Wrap-up

Once every setup task is done, closed, or waiting only on the person, schedule the daily health check: load pack `automations` if needed, call `automation_list`, and if no active schedule has payload `{"kind": "corsia_agent.health_check"}`, call `automation_schedule` with `kind: "cadence"`, `spec: "86400"`, `max_runs: 1000` and that payload. Then send one `post_update`. A task waiting on another agent (a project's conductor) is not settled yet. It opens `Setup's done.` when nothing waits on the person, otherwise `Here's where setup stands.` with one `Still waiting on you: [person task titles]. Tell me when, or say not now.` line. Then one clause per thing set up, only what is true, the health check, and when to come back:

> Setup's done. [Project]'s conductor proposed how it should run: [document]. Web search works. You can open CORSIA on your phone at [address].
> Once a day I check that your models, tools and keys still work, and I tell you only when something needs you.
> Come back when [the next thing that will need them: an agent's first result, a proposal to approve, or a decision], and it will be waiting here.

Add the education close from `references/first-agent.md` (context, self-optimizing agents, the Measurement dashboard) after the health-check line, and what to come back to me for.

If scheduling failed, say so instead. A declined item gets no clause. After the wrap-up, a step finished later gets only its own result line, no second wrap-up.
