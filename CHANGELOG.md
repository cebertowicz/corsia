# Changelog

Shipped behavior, newest first. When a later entry changes an earlier design,
the later entry describes the current behavior.

## v2026.10.01 — Plan limits hold and release work cleanly

### Providers and Models

- Usage limits are tracked per account and per model tier: a tier that is still allowed never clears an account-wide limit, and a weekly tier limit holds only that tier.
- A rate-limit rejection outside a named plan window is recorded as an observation, no longer as a limit that holds work.
- Work held by a limit is released once, when that limit resets, and a release that fails now shows as an error instead of passing silently.
- Reaching 100% of a plan window holds new work on that account and pauses each running turn once; its unsent input returns to the queue and the thread resumes on its own after the reset.
- A per-account switch to keep spending past the plan limit is available through the connection usage API, off by default; turning it on releases held work.
- Stored limit records move to the per-account and per-tier form: the schema rises from 75 to 76, migrating automatically at startup.

### Reliability

- Repository tests no longer fail on a slow or busy machine, and a concurrent continuation test waits for both runs — determinism fixes for the backend suite.

## v2026.09.30.1 — Document collaboration ships, plans say their times, Focus sharpens

### Documents and Artifacts

- Document collaboration is live: comments open beside the text they quote, agents suggest edits and the document's owner accepts, rejects, or accepts all — an accepted suggestion saves under the proposing agent's name — and presence shows live cursors, avatars, and an arrival reveal when someone joins.
- Feedback on a document reaches its owner as one plan task with Send, Retry, and reply controls; a batch that never delivered is announced and an agent can retry it, and delivered edit-only batches stay safe to retry; non-owner edits reach the owner as queued feedback; a comment with no stored context never lands on different words.
- Every shared document has one owner, and the gate enforces it: existing documents gained an owner at assignment with their verified-at dates imported, and owner and review age now show wherever a shared document is read, alongside the freshness UI and the label that names the action that clears a never-verified state.
- Writes from a shell or an external process are no longer misattributed to whoever last opened the editor.
- Collaborating on documents stores suggestions, feedback batches, and non-owner edit records, and checkpoints gain a history-end field: the schema rises from 69 to 75, migrating automatically at startup.

### Plans and Coordination

- Waits say their time: each waiting row shows when it ends, and a wait past its deadline counts as work not moving.
- The status says when an owner owes you a verdict; only work actually moving reads as Working, and the plan header lives with the surface hosting it.
- When a delegated worker's wait clears, its parent reselects the step automatically — no extra wake needed to resume the work.
- Worker returns that land after the parent already settled are harvested and settled with the parent's verdict.
- Refusals explain themselves: a start or mark-done blocked because the task slot is taken names the holder, and arming a deadline reads as scheduling; a refusal names both supported fixes.
- Integration sessions now run against one workspace root, so a test batch that follows another suite no longer misroutes file permissions — a determinism fix for the backend integration suite.

### Chat and Interface

- Focus mode is send-only: it shows your messages, the updates agents send you, and plan decisions; working steps tuck behind a single summary line.
- Agent rows carry a one-line summary of their newest turn, and Focus pages to whatever is actually visible.
- A stalled lane is labeled Stuck, and update cards now show, stay pinned, restore their newest window, and keep landing under your control — with the visit divider and card store hardened.
- The live step survives switching between rows, attention actions are instrumented for the cost-per-outcome measure, and the reply-window projection is gone.

### Context and Memory

- A checkpoint records when its history ends, and older checkpoints show their dates on one line.
- The recovery hint names checkpoints, not thread summaries, and the model label stays readable in a narrow composer.
- The default coordination model's memory row names message and checkpoint search, replacing a layer that no longer exists.

### Providers and Models

- The model picker groups by account and shows each model's usage.
- Usage windows merge sparse observations field by field, so a partial read no longer overwrites a sharper one.

### Agents and Automation

- Process cleanup reaps in the right order, so stopping a managed background process can't race its own children.
- One file's write root resolves without listing every worktree.

### Reliability and Contributor Surface

- The contributor backlog tooling keeps its retention sweep going past a stuck tree, preserves mint errors, sniffs HEIC/HEIF/TIFF uploads as images, and fixes its worktree repo lookup and issue catalog link.

## v2026.09.30 — Model availability you can read, plans that finish themselves, jobs that run for hours

### Providers and Models

- One owner reads model availability for a connection: observed usage windows and the vendor's verdict are stored together. Only a limit that is actually active blocks a model — an expired window reads as unknown and the model stays usable, the latest observation wins, and a live event breaks the tie.
- The model catalog shows each row's usage state, and listing models reads it once for the whole listing instead of once per row.
- A delegation aimed at a model that is ineligible or currently usage-limited is refused with the reason and the alternatives that are ready. It no longer switches to some other model quietly.
- A rejection that belongs to one Claude model no longer marks every model on that account as limited.
- Codex's model list refreshes when the tool is rediscovered, and Claude context-size discovery gives up after a bounded set of retries instead of repeating itself.

### Plans and Coordination

- Plan status now has four states: running work reads as "working", the node, the plan, and the details pane agree on what actually needs you, and a decisions-only launcher is labeled as decisions instead of a second "needs you" count.
- Expanding a task row is read-only detail: working state, tokens, and priced cost are shown as separate facts, partial measurements read as a lower bound, and lifecycle actions stay in the row menu.
- An agent settles a task it finished with Mark done, backed by evidence — the published first choice. Review-then-accept still works as the compatibility route.
- An answered plan ask stops reappearing: attention closeout is the one owner that releases a linked ask, and a retired artifact clears its ask before archiving.

### Chat and Interface

- While you are away, agent rows and conversation rows carry a one-line summary of their newest update, so returning shows what happened without opening anything.

### Context and Memory

- Records stored under the old project layout are converted before paths resolve strictly, so existing conversations, summaries, and context keep resolving, and a project-root picker covers the move. Dropping journal columns that were never written raises the schema from 68 to 69, migrating automatically at startup.
- The context meter and the context sheet now agree, and every estimate is labeled with what it actually measures.
- A slow context preview records its timing by stage, so production data can name the stage that is slow instead of guessing.

### Documents and Artifacts

- A pull request closed without merging retires its registered artifact and is recorded as an informational close event, and a one-time reconciliation applies the same retirement to artifacts whose close was never recorded.

### Agents and Automation

- A background process an agent starts can opt into durable ownership — 2, 6, or 24 hours — with truthful behavior for completion, failure, timeout, restart interruption, deletion, retained logs, and unrecoverable death.
- Input sent to a delegated native helper now reaches it instead of being dropped.

### Reliability and Contributor Surface

- A canceled or timed-out CI job no longer orphans its server processes on the self-hosted runner, and the API-version regression proof waits on real synchronization instead of wall-clock luck.
- An evaluation case that refuses an oversized input now fails its test instead of releasing the input into an endless retry chain.
- The contributor skill index names, per harness, the read tool that harness actually has.

## v2026.09.29.2 — Comments stay on their words, and an agent's answer reaches you

### Documents and Comments

- A document comment now travels with the text it quoted, no matter how the file was changed — the editor, an agent tool, a shell write, or git. When the quoted words are gone, or appear more than once with surroundings that can't tell them apart, the comment shows its orphaned state instead of landing on different words. Editing inside a commented span orphans that comment; restoring the same words re-attaches it.
- A comment's anchor now carries up to 32 characters of the text around it. Storing that adds two columns to comment storage, so the schema version rises from 66 to 67 and migrates automatically at startup.
- Opening a document's comments no longer stalls: 50 comments on a 200 KB document list in milliseconds, and the listing says which threads are orphaned and how many anchors were checked.
- Known limitations for this behavior: comments recorded before this release have no stored surroundings, so a repeated quote resolves to the nearest occurrence; and while an edit is still unsaved in the editor, a comment can read as orphaned until the document is written.

### Chat and Delivery

- Long conversations keep their recent memory. A checkpoint now lands at a warm position before a split provider message, prefers the previous turn's end over the running turn's own input, and a freshly started Codex process picks up warmth from its recorded session — so fewer turns have to be rebuilt from cold storage.
- Every message you send now carries the standing instruction to answer with a visible update, so the reply reaches you in Focus mode. The instruction is stamped when the message is admitted, so older messages keep the text they were stored with. An agent that settles four tool results without sending you anything gets one gentle reminder per turn.

### Context and Guidance

- The precedence contract agents are given is now explicit: workspace, project, role, and thread context may add requirements or restrict what may be done, but anything that removes, skips, or weakens a System requirement is ignored — including an instruction to work without a structured plan.

### Operator and Developer Surface

- Message formatting is split into modules with one owner each — display text, model history, tool lineage, reasoning provenance, and row primitives — and the dead code it had accumulated is gone. No behavior change.

## v2026.09.29.1 — Waiting work resumes itself, delegated work reads as current, and Measurement gets its first page

### Plans and Coordination

- An event wait always carries a deadline, and a step waiting on a subagent that has stopped is heard instead of held open indefinitely.
- A scheduled wait that goes idle says how to resume it, and idle scheduled waits resume themselves.
- Nothing stays marked in progress under a step that has finished.
- Mark Done or Won't do from any row; one tap on Won't do also clears a decision task, and work sent back for more still isn't a failure.
- A refused task control now says why beside it instead of only refusing.
- A collapsed row holds finished work only, and the live step moves between states without a jarring redraw.
- A parent step is dated by its most recently finished child, not by its own last edit.
- A tool result is bound to the exact Assistant Step that produced it.
- Delegation reads truthfully: delegated work shows as the current task, a delegated review's attention is surfaced, a reused worker takes a named plan task, and one answer settles whether a worker still has work open.
- A turn-end retry can no longer repeat a write that already committed.

### Chat and Delivery

- The "For you" marker is now labeled Update, with a dot and seen-state that track what you have read.
- Links open where their target lives — the browser or the artifact panel.
- Focus mode keeps your presence line in view after a mid-turn checkpoint, and presence is recorded for the chat you actually have open.
- A document's repository-link picker moves into its own panel.

### Context and Memory

- Deleting a thread also removes its checkpoints, staged edits, and continuity entries.
- A conductor can edit an agent's instruction file in the product, and every save keeps its history.
- Cold context preview is fast, one source feeds both the compaction trigger and the context numbers, and a provenance read that fails is reported instead of breaking the panel.
- Context-file registration failures are logged and counted, and idle-marker counts bind to the clear that actually committed.
- Tool descriptions match what the tools do, and a lesson retires only once it verifiably landed where it was meant to go.

### Delegation and Agents

- A delegated thread must be given its model explicitly; nothing runs on a hidden default.
- A session poisoned by an empty text block is retired instead of resumed, and a toolless bridge is refused with the tool list retried once on rotation.
- An idle session whose compaction window changed is rebuilt so its next input lands.
- Stale-context raises log their site again, and live compaction overrides are compared as a whole setting rather than field by field.

### Models and Providers

- Legacy core model settings are retired; Claude model and context discovery now work without operator settings.
- The compaction trigger and install guard read their configuration once instead of repeatedly per turn.

### Measurement and Workflow Evidence

- A Measurement page and its API show whether a change actually moved the numbers, and a seeded fixture makes those numbers reproducible.
- An evaluation turn opens its own thread, reads its replay, and runs the same turn and step path as ordinary work.
- Trend charts scale below 1, "not measured" gets its own mark, and a third series meets 3:1 contrast.
- Evaluations run end to end: the runner restricts which tools an eval may use, meters its spend, and reports which route fit which job.

### Guidance and Coordination Packages

- The default coordination model ships as a package: charter and safety templates, pinned refs, KPIs, and its first bundled test.
- Conductors can read backend logs; the bootstrap line names only the bootstrap skill, and model names live in one place.

### Operator and Developer Surface

- An Update now installs locked frontend and Python dependencies.
- `anyio==4.13.0` is declared as a runtime dependency instead of arriving transitively.
- `/api/version` reports unknown rather than dirty when git reads fail, and the dev frontend refuses to start without an explicit backend URL.
- Test suite stabilization: lifecycle tests no longer depend on host sign-in state or wall-clock bounds, browser-close tests release their fixture daemon explicitly, and tests that used to time out under full-suite load now pass.
- The identity rename's groundwork lands: persisted identity seeds are frozen ahead of it, with a name-map tool and a hardened policy.

## v2026.09.29 — Work that reschedules instead of stalling, chats that keep their memory on the move, guidance that keeps its history

### Plans and Coordination

- Finished work lands as Done rather than Closed, and an accepted step settles in one call when it carries evidence.
- Work sent back for more reopens without ever reading as a failure.
- A step waiting on an agent, the system, or an outside party now expires on a deadline and wakes someone; only steps waiting on you stay red.
- Agents can no longer pause work indefinitely: they schedule a step until a chosen time with a stated reason, and only you can pause one. Scheduled steps show their time and can be snoozed.
- Right-click a step to pause it, resume it, or mark it as not happening.
- A plan says plainly at the top when nothing is moving, and what would move it.
- Waiting time now shows beside working time on a step.
- An agent with an empty slot is shown its waiting steps, soonest first, each with the reason it waits.
- A worker swapped out mid-task wakes its parent when it returns, the same as the original worker.
- A reused worker's plan shows under one step only, so its nesting and cost are not counted twice.
- A parent can accept its delegated step once its worker is idle or unreachable, which closes the worker's open steps with a stated reason.
- One plan can read another thread's plan in the same project, and a step can depend on a step in another plan and wake when it settles.
- Deleting a thread takes its temporary subagents with it, and nothing adopts the deleted thread's work.
- Settled rows say when they finished — "today at", "yesterday at", or a date — in your own time zone.
- A wrapper step now stays visible when it carries state of its own, instead of being folded away.
- Plan writes no longer hold the database lock for seconds, so the API stops stalling behind them.
- A plan write refused by the lock at the end of a turn is retried instead of lost.
- Liveness sweeps run again after a crash: a check for a silent worker now opens its own database read.
- A worker's silence clock counts only the worker's own output, so incoming messages no longer reset it.

### Chat and Delivery

- Focus mode hides an agent's finished working steps behind one summary line. A running turn still shows every step, and your own messages and marked updates always stay visible. It is on by default and switchable.
- Agents are told what Focus mode shows you, so they stop putting what you need to see into steps you would never read.
- A message sent to a conversation with no model chosen is held in its queue until you pick one, instead of failing or silently running on a hidden default.
- A queued message whose model is unavailable keeps its exact place in line and runs when that model returns, showing "Waiting for model" rather than a failure.
- An accepted message no longer reports failure because a later bookkeeping step stumbled.
- A turn cancelled while it was still setting up now settles instead of lingering.
- A background fork that runs out of its budget fails with a named cause instead of dying silently.
- During a restart drain, a refused action names what to do about it, and the resume path stays reachable while draining.
- A checkpoint no longer times out waiting on slow context preparation.
- A resumed snapshot names the message it resumes from on both external harnesses, and a refused capture says which check rejected it.

### Context and Memory

- Moving a chat to another project keeps its memory, provenance, search index, skills, and per-panel context.
- Each open chat panel shows its own context usage.
- The packaged System context is a smaller, portable core that arrives first in a turn and survives a checkpoint.
- A checkpoint's search index holds the narrative only, so recall stops surfacing bookkeeping.
- A turn whose provenance cannot be read is reported honestly instead of breaking the read.
- History search results say what kind of sender each came from.
- A recall promise at the start of a step names the read it will make, not just the step.
- A lesson retires only once it verifiably landed where it was meant to go.
- Removing an agent from the roster keeps its responsibility record so it can be promoted again later.

### Guidance and Workflow Evidence

- Every guidance save becomes a Git commit recording who saved it and why — through the file tools, the document editor, and the lesson-placement pass alike.
- Workflows are versioned automatically from the skill that actually resolved; a caller can no longer set its own version.
- A read-only per-workflow-step report shows cost, time, attempts, and model attribution, and marks a rollup that disagrees with its parts.
- A step carries the workflow and difficulty it ran under, with a non-blocking notice when a required step is missing.

### Interface and Settings

- Provider settings list only the models the account can actually offer, show the checkpoint window for every model, and drop the sampling editor.
- The workspace view gets its own settings, and system settings no longer hold context or skill files.

### Reliability and Operations

- Reading the running version no longer takes the serving checkout's lock, so an update stops failing on a leftover index file.
- Light checks run on the local Mac runners while GitHub's hosted budget is out, so a pull request still gets lint, types, anti-pattern, docs-drift, and frontend-unit verdicts.
- A hung backend test shard prints its log when a CI job times out, instead of failing with no reason.
- Duplicate reviewer check runs are collapsed, so an old cancelled run can no longer veto a newer passing one.
- The chat list's plan status skips the cost projection it was computing and then discarding.
- A browser close retry waits for a close already in flight instead of racing it, and a half-read process record reads as an exit rather than a live process.
- A GitHub notice survives a failed delivery and is carried by the next event, and a quiet-watch read restarts rather than stranding its claim.
- The overnight trim of old tool payloads no longer holds the database writer for tens of seconds in one pass, so turns are no longer killed by a lock while it runs; it now bounds each write by size and time as well as row count.
- When the protected shell cannot start, the refusal now names the location it searched and the two ways to recover it, and service start, stop and restart say plainly which of them keeps your installed definition.

## v2026.09.28 — Models that say when they cannot run, plans that clear themselves, turns that finish

### Providers and Models
- A model whose account has hit its usage limit now reads as unavailable, and returns to the picker when the limit clears.
- A conversation pinned to a renamed model keeps running on it, and the picker lists each model only once.
- Opening an existing conversation shows the model it is actually on, not the one from the last thread you viewed.
- Cloud endpoints you added are no longer probed in the background, and their reasoning levels come from the public model catalog.
- Providers and models can be managed from the shell, and those routes accept a bearer token instead of a token in the address.
- A long status line no longer squeezes a model's name out of view, and each setting switch says what it does.

### Plans and Coordination
- Parking a task frees the agent to take its next one at once, instead of holding it to the end of the turn.
- Delegated work now belongs to the task it was called for, so a worker's plan reads as part of that task.
- A task with a worker running reads as in progress everywhere, including when a check-in watch sits on it.
- Closing a task withdraws the decisions under it, and a withdrawn decision closes its task rather than failing it.
- A step replaced by newer work closes quietly instead of counting as a failure.
- A worker that stops with work open, goes quiet mid-turn, or asks a follow-up now wakes its parent.
- Resuming an agent leaves its already-finished workers alone instead of waking all of them.
- Finished work folds away only after you have seen it, and only what is new since your last visit stays on screen.
- Closed tasks stay on the plan with the reason they closed, folded into their level's count.
- Node details are compact: one status line, one cost line, the detail behind each, and no repeated header.
- Wait times and deadlines read in your own time zone instead of a machine timestamp.
- Marking a task done settles it even while it waits on a watch, a dependency, or another actor.
- A plan argument written as plain JSON text is understood, so fewer calls fail on formatting alone.
- A nested worker's plan drops the empty wrapper step that only buried its real work.
- Plan updates no longer hold the database lock for seconds, and a sweep opens the writer only when it has something to wake.

### Chat and Delivery
- A dormant conversation answers your next message in the same turn instead of showing a retry you had to press.
- A message sent while the agent is writing its final answer is queued and answered, never quietly lost.
- A provider stream that ends early or goes silent now retries the call or reports an error, instead of stopping mid-thought.
- A tool call that already completed keeps its result even when the bookkeeping after it fails.
- Idle and pressure-triggered checkpoints reuse a warm snapshot or skip it, rather than forcing a slow cold rebuild.
- A turn no longer offers an agent tools that it could only refuse.
- An agent's posted update reaches the open conversation and stays tied to the turn that wrote it.
- A turn whose process has stopped is reported plainly instead of looking like it is still running.

### Interface and Navigation
- Each row in the list shows only what you can settle in one tap, and a failure mark clears once you have seen it.
- A collapsed group of tool calls shows when one of them failed, and every tool call shows its result.
- A plan item shows the step it is on while its work is running.
- Clicking an attention item brings it into focus, and its phone target is large enough to tap reliably.
- Approvals waiting on you for machine access now appear in the attention list.

### Context and Memory
- Every turn records the guidance versions it was given, and that record survives later changes to the sources.
- Placing lessons at a checkpoint runs on the conversation's own model instead of a weaker background one.
- A restored checkpoint lists its stored artifacts by an unambiguous identifier.

### Agents and Automation
- A wake is recorded as delivered only when the agent actually took it in, and a refused wake is reported rather than dropped.
- A wake that dies after being accepted is handed to the health owner instead of disappearing.
- Notifications cover only the repositories you track, retry a briefly unreadable payload, and name the real cause of an auth failure.
- A pull request whose CI result never arrived is detected from the delivery log, and a cancelled run never counts as a pass.
- Every project agent can reach the workspace's coordinating agent, and that agent can reply.
- Demoting an agent from the roster keeps its responsibility record, and roster edits carry their own revision.
- Shared system skills are readable by every agent, including one running in a protected shell.
- An agent can open a browser window you can watch, when you ask; browsing stays hidden by default.
- An artifact asked for by relative path resolves against the agent's workspace, or fails loudly.

### Reliability
- Startup no longer crash-loops the server when it finds work that was closed mid-call.
- The nightly cleanup no longer deletes files that running threads still need.
- A merged branch's working copy is removed at merge time instead of lingering.
- A protected shell command no longer re-scans every working copy on start, and a slow one now says which stage took the time.
- Loading an image with a message no longer occupies a database connection while it is resized.
- Message trimming runs in bounded batches, and queued database work can no longer cross a database swap.
- An interrupted checkpoint-search upgrade now either completes or leaves nothing behind.
- The code review gate falls back to another model when its first choice is at capacity.
- A slow repository operation returns a retryable answer instead of a hard refusal that nothing was pinned.
- Contributors see the blocking checks before they push: the local gate now mirrors the CI suite, and the pre-push hook runs it.
- Anywhere the product called a conversation a "session" now says "thread", and its API routes moved to match.

## v2026.09.25 — Model cards you control, working copies that tidy themselves, and a shell that starts at once

### Providers and Models
- Settings gains a card for each coding tool: choose which of its models the picker offers, and rename the ones you keep.
- A hidden model stays hidden when the tool is rediscovered, and a conversation pinned to it keeps running.
- A mistyped endpoint address now fails at save or test, naming the cause, instead of failing on your first message.
- Editing an endpoint while a check runs no longer leaves a stale failure on an address that works.
- External models work out for themselves whether they take images, and your own choice survives the next rescan.
- An open model list reloads after a tool update adds models, and listing models no longer triggers a version check.
- Switching an external model on or off behaves like any other model, and load and unload refuse it cleanly.
- A fresh install can hold its first conversation before a core model is set, and says where to set one.

### Plans and Coordination
- A finished task stays on the plan until you have actually seen it, then folds away on a later visit.
- Starting a task surfaces related past work, so settled decisions stop being relitigated.

### Chat and Delivery
- A message you send while a reply is still arriving waits below it, and stays there.
- A delivery receipt that arrives after its turn ends settles back into normal transcript order.
- Opening a long thread shows its first page in milliseconds rather than seconds.
- A thread that does not fill the screen on open loads older messages by itself.

### Interface and Navigation
- Dropped files attach to the conversation you are composing in, including a delegated one.
- A delegated helper shows under the row that owns it, and only that row moves while its turn runs.
- A lease chip no longer draws a clipped shadow, and a new chat no longer opens with a failed-load line.

### Context and Memory
- A conversation summary carries its artifacts and plan state, so work resumes with its context intact.
- The context scope that applied to everything is named after the workspace instead; the old name still resolves.
- A lesson lost in the fold that was meant to carry it is reported as unplaced, not written off as done.

### Agents and Automation
- An agent can post an update addressed to you without derailing the reply it was writing.
- Agents can drive a real browser again on the newest macOS, after a launch failure and a version gate were both fixed.
- Turns with several tools running at once keep their context warm, and a new process skips a rebuild it cannot use.
- A refused context refresh logs its reason, so a cold restart has a readable cause.
- A turn whose tooling cannot start reports the server's status and how long it waited, and finishes instead of going quiet.

### Governance and Safety
- Agents running in a protected shell can no longer read your clipboard.

### Reliability
- Protected shell commands start in about a second, however many working copies the project holds.
- Working copies are cleaned up automatically once their pull request merges, and anything dirty, recent, or in use is left alone.
- An interrupted repository update no longer stays locked forever; a provably stale lock clears and names one safe next step.
- A record left by a closed pull request no longer parks a working copy out of cleanup indefinitely.
- A leaked process from a stopped turn is reaped instead of stalling a test runner for fifteen minutes.
- Request and tool timings are recorded, so a slowdown is measured against history rather than guessed at.

## v2026.09.24 — Delegated work comes back, pauses mean it, endpoints are editable

### Plans and Coordination
- A delegated step now reports back when the worker's turn actually ends, so a result can never read as arrived before it exists.
- A delegated step that never started still returns, and closing a step closes whatever its worker still had open.
- After a rejection, the revision you asked for carries through to the worker instead of the parent having to re-explain it.
- Only you can resume a paused plan; an agent can no longer talk itself back into work.
- Pausing now reaches every worker underneath it and stops a running turn at a safe point rather than mid-write.
- Everything a paused agent receives opens with the pause line, and automatic inputs land in a digest instead of appearing to have run.
- The task an agent was asked to do is never handed back to it as its own next step.
- Work waiting on a person or another agent reads as waiting; "blocked" now means exactly one thing everywhere in a plan.
- A watch that nothing could ever end is refused when created, with the reason.
- Finishing a task carries the evidence it was accepted on, and concurrent updates can no longer fight over a task's state.
- The plan header shows a single count of unfinished work, rows open their own details, finished branches fold away once you've seen them, and archived workers keep a readable plan.
- The chat list shows each agent's state and marks work moving through a worker, without the dot that read as an unread message.
- Plans cost measurably less to run: tool schemas are 47% smaller, writes journal once per change, usage totals are summed in the database, and evidence reads are bounded by task count.
- Plans imported from a markdown file are gone; the structured plan is the only plan surface.

### Chat and Delivery
- Status chips in live chat keep their order no matter which producer emits them, so a turn's story stops rearranging itself.
- A finished tool that failed keeps its real cause instead of collapsing into a generic error.
- A turn whose model has become unavailable now ends with a clear message instead of sitting there.
- Codex turns match results to calls reliably again, and a usage record arriving during a brief database lock is counted once rather than lost.
- Stopping a Codex run no longer sends signals to a process group that was already killed.
- Cold checkpoint recovery now fits an oversized conversation, and native checkpoints reserve their output room before starting.
- Finding a step inside a very long thread no longer reads the whole transcript.

### Context and Memory
- Agents can ask for a checkpoint themselves, and idle checkpoints now work for ordinary top-level threads.
- A passive event no longer wipes the marker that keeps a session warm, and every decision to skip an idle checkpoint is named in the log.
- Context installed mid-turn is fixed: static context now lands only at checkpoint boundaries, so an agent's view stops shifting under it.
- Checkpoint history can be searched across compaction epochs, and opening a thread no longer loads an unbounded checkpoint list.
- Tokens spent on delivered context are accounted for rather than estimated.

### Providers and Models
- External endpoints are editable in place, show what discovery actually found, and support two providers sharing a model name.
- A saved endpoint key stays with its host, and a stale discovery sweep can no longer overwrite your edits.
- Cloud external endpoints group under their provider with a usable model list and readable addresses.
- Models a harness has just released are picked up on restart and within an hour, and re-discovery no longer runs in the middle of your request.
- Provider settings now explain why an enabled model is missing from the chat picker.
- No sampling or output-cap parameters are sent to any model, and context and cache windows are labelled as the checkpoint settings they are.

### Governance and Safety
- A refused host-access request now says which rule it broke and what to change, instead of a bare rejection.
- Routine work by an owner is approved automatically again, while leases that genuinely need your decision still stop for you.
- Audit reads are scoped to a bounded window, and a long recovery query can no longer run away.

### Interface and Navigation
- Project and conductor settings are reachable from the header again.
- Decision and access drawers and their backdrop cover the whole app instead of one region.
- A delegated conversation opens beside the thread it belongs to.
- Side drawers keep consistent padding across window sizes.

### Agents and Automation
- CORSIA can receive GitHub comments with no public webhook to configure.
- Pull-request activity now wakes the owning thread quietly, and a thread claims pull requests it owns from their own ownership lines.
- Reading a registered source repository that has gone stale now warns instead of quietly returning old files.
- Conductors can message each other natively, scoped roster tools exist, and a designated conductor is created for a scope that lacks one.

### Reliability
- Database connections are released when a worker thread exits, and a forked process can no longer release connections it does not own — the cause of a class of unexplained write stalls.
- Native browser use works again, and agent shells can launch a headless browser.
- Reviewer runs skip draft pull requests, and stale review threads resolve themselves instead of lingering.

## v2026.09.19.2 — Why a checkpoint was skipped, stated precisely

### Context and Memory
- When a background checkpoint doesn't run, the reason is now recorded precisely — too small, no boundary, a turn in progress, already covered, or nothing to build from — instead of a generic refusal.

### Chat and Delivery
- A database connection used to look up a thread's project is now released when the query fails, so repeated failures no longer hold resources.

## v2026.09.19.1 — External MCP tools everywhere, Codex stop and receipt reliability

### Providers and Models
- Tools from the external MCP servers you configure now reach every harness, instead of only some of them.
- An MCP server that fails to start contributes no tools and logs a clear redacted error, rather than degrading the others.
- Forced Code Mode is cleared from the managed model catalog, so CORSIA's own tools are usable again on those models.

### Chat and Delivery
- Pressing Stop on a Codex run now releases the processes it started, instead of leaving them holding a write slot.
- A Codex tool result is matched to its call reliably again, so turns finish with real receipts instead of correlation errors.

### Governance and Safety
- A valid host-access decision is kept when browser isolation fails; the lease stays unusable until isolation succeeds, and the message names the real cause.

### Getting Started and Contributing
- Added a private Tailscale guide for reaching CORSIA from your phone, and corrected the prerequisite that left user-installed command-line tools invisible to background services.

## v2026.09.19 — Agent capacity that frees itself, clearer event chips, tool mistakes that explain themselves

### Plans and Coordination
- A delegated step waiting on its own owner's verdict no longer reads as if the work came back, so a result stops skipping a level in the chain.
- Idle background agents are now retired automatically, so a day of normal work no longer fills the delegation limit and blocks new delegation until a restart.
- When delegation is refused, the message says what is actually being counted and lists the agents still working.

### Chat and Delivery
- A misspelled CORSIA tool call now settles as one clear tool error instead of hanging or duplicating.
- When a Claude session is slow to admit a cold start, you get a visible refusal naming the timeout instead of silence.

### Context and Memory
- Each context event gets its own chip line, context chips are flattened, and chip times read correctly.

### Getting Started and Contributing
- An issue a pull request says it fixes now closes when that pull request lands on the working branch, not only on the release branch.

## v2026.09.18.1 — Calmer delegation, faster context sheet, clearer access decisions

### Plans and Coordination
- Finished plan work folds away on its own, with one clear control to bring it back.
- A worker that dies, blocks itself, or finishes its last step now reports back instead of going quiet.
- A result queued for a waiting task now arrives on its own, instead of sitting there until your next message.
- Waiting is quieter: Stop ends the right wait, a finished review releases the task waiting on it, and waits show their age.
- You can wait on a worker that's still running, swap out a worker that isn't making progress, and adopt a worker whose delegator is gone.
- A delegated result now reaches its parent exactly once, never as two competing messages.

### Context and Memory
- The context sheet opens on the last usage it measured and refreshes in place, instead of blanking to a spinner every time.
- The context sheet now says whether a checkpoint is due, and drops a notice that was no longer meaningful.

### Chat and Delivery
- Answering a decision now supports a multi-line free-text answer, and the recommended choice is marked on its option.
- A tool call with the wrong arguments now returns a proper error instead of leaving the chat waiting on nothing.
- After a restart, tool calls that can no longer be matched end clearly instead of hanging.

### Governance and Safety
- When a command is refused, you now get the actual reason and what to do about it, instead of an unexplained block.
- Multi-line scripts reach the access reviewer as written, so valid code isn't rejected as a syntax error.
- Read-only checks inside an approved area are allowed without needing each one spelled out in the plan.

## v2026.09.18 — Quieter delegation, tool-load efficiency, usage durability

### Plans and Coordination
- A delegated worker's ordinary progress now reaches you as a quiet update; you're only woken when a step actually needs your judgment.
- A step that keeps failing on a delegated worker now wakes you once, at its third attempt, instead of retrying silently forever.
- Work done with no task in progress is now counted, not silently unmeasured.

### Context and Memory
- Threads now load only the tools each turn actually needs and pull in the rest on demand, cutting wasted tokens on long conversations.

### Chat and Delivery
- A usage record that arrives while its database is briefly locked is no longer lost; it's counted once the turn ends.
- Switching into Codex from another harness during a model switch no longer fails outright.

### Data and Migrations
- Fixed a data-migration script that could refuse to convert two older record shapes it didn't recognize.

## v2026.09.17.2 — Image-send stall fix, stop and switch reliability

### Chat and Delivery
- Sending a large image no longer ties up shared database workers and stalls unrelated work.
- Pressing Stop now ends the wait it was blocked on, and says why, instead of a generic failure.
- A failed model switch keeps its original error, so the cause is still there when you look.
- A model switch that races with other work now retries instead of failing, and a failed switch wakes the conversation instead of going quiet.
- A background context update no longer blocks a model switch; it carries over to the new model.
- A delegated thread that was relaunched no longer wedges, and its parent still hears from it.
- A single stale "waiting for retry" record could stop the backend from starting at all; that dead state is gone.

### Context and Memory
- The context view now accounts for the tokens Claude Code and Codex hold internally, instead of explaining only part of the total.

### Plans and Coordination
- A task's cost now counts a measurement that hasn't arrived as unknown, rather than as free.

### Getting Started and Contributing
- The setup pack installs from the released branch, and walks through troubleshooting before asking for help.
- Added a release-time check that keeps the published setup docs in step with the code they describe.
- Removed personal paths and internal working scratch from the repository.

## v2026.09.17.1 — Conductor model picker, plan costs, usage accuracy

### Chat and Delivery
- The model shown when you open the conductor is now the conductor's own model.

### Providers and Models
- Corrected Claude Code output-token counts, which were understated by roughly twenty times.

### Plans and Coordination
- A large plan now reads in bounded pages instead of one oversized reply.
- A rejected plan update now states the valid shape and the fix instead of a generic error.
- Any task in your own plan can be closed, and several tasks can be archived at once.
- A task waiting on work that is still moving now reads "Waiting", not "Blocked".
- Each plan task reports its own token and time usage, with the total shown once at the top.
- The first start after this upgrade runs a plan-schema migration and takes about half a minute longer.

### Getting Started and Contributing
- Split the offline setup guide into a short path to a running conductor and a longer setup reference.
- Added setup guidance for phone access, the desktop app, and the conductor's own project setup.

## v2026.09.17 — First release to main

### Getting Started and Contributing
- Rewrote the README around what CORSIA is, what it needs, and how to run it.
- Added an offline setup pack so a new contributor can install and run without a live connection.
- Published the standards the automated reviewer applies, so contributors can see the rules they are judged against.
- Documented the labels and conventions that govern outside contributions.

### Plans and Coordination
- Replaced the Plans home page with an Activity view and contextual, per-thread plans.
- Plan status now shows a compact summary of current work and recent outcomes, not every node.
- The plan pane shows one clear heading instead of repeating across the transcript.
- Delegated work shows its child plan's progress live and captures the result when the worker returns.
- Closing or parking a task now reports what comes next, and can start it directly.
- Users can mark their own assigned tasks done or "won't do"; agents no longer touch a user's tasks.
- Plan nudges now fire only for work that can actually move, once per state.
- Waits on events, reviews, or external input are durable and are not nudged while pending.
- Plan decisions are delivered and answered through the same message queue as everything else.
- Plan tasks now require verifiable evidence before completion, with explicit holds and blockers.
- Fixed a plan-schema migration bug that could turn a failed upgrade into a full outage.
- Fixed several cases where a plan's tasks, watches, or archived branches could get stuck.
- Workspace-level threads can now become persistent Agents, not just project threads.
- Persistent Agents and Conductors now receive their role's context automatically.

### Chat and Delivery
- Sending, stopping, retrying, and steering now behave consistently across Native, Claude Code, and Codex.
- Fixed a broad set of turns that could hang instead of finishing or returning control.
- The live "thinking" indicator no longer goes quiet while a tool is still running.
- Stop and Retry are now reliably reachable and behave as distinct actions.
- Editing an earlier message now updates every later row consistently on every harness.
- A blocked message now has one clear way to resolve instead of getting stuck.
- Agent-sent Markdown, including lists and wide tables, now renders correctly on mobile.
- Fixed live streaming showing only a typing indicator until the thread was reopened.
- Switching threads no longer briefly shows the previous thread's transcript.
- Chat history now loads roughly half the data it used to, with no visible change.
- Read-file results can now be delivered as images to harnesses that support them.
- Stop now always ends the turn and leaves the thread usable, on every harness.
- A thread whose history is too large to compact now answers or visibly fails instead of going silent.
- A tool call CORSIA does not own now settles on its own instead of ending the turn.
- Compaction accounting no longer stalls the server while it runs.

### Context and Memory
- Context-window handling near a model's limit is now consistent across every harness.
- Agents can recover an earlier decision and its reasoning from a past checkpoint without rereading history.
- Per-call usage and cost figures are now accurate and attributed to the correct task.
- A model call whose usage cannot be recorded now shows as a visible defect.
- Checkpoint continuity notes now render as compact, readable text.
- Checkpoints now carry an index of prior continuity entries so later turns can find them.
- The context-usage percentage is computed once, so the chip and detail sheet always agree.
- A changed bootloader file is now noticed once, however it was edited.
- The continuity editing tool now advertises exactly the fields it accepts.
- Editing and resending a message to a local model now preserves its cache.
- Checkpoint search is now indexed for faster lookups.
- Managed Claude Code sessions now load only the context CORSIA delivers, never the operator's own files.
- The context sheet now names the budget it counts against and says when a compaction is pending.
- Idle checkpoints now run while the model's cache is still warm instead of up to half an hour later.
- A checkpoint can no longer install with an empty summary and discard the history it replaced.
- A model switch at a tool boundary now survives the original caller disconnecting.

### Documents and Artifacts
- Removing an artifact now shows as one grouped event in chat instead of scattered notices.
- Artifact ownership tracking works automatically again after a regression.
- Artifacts tied to a finished plan task are automatically retired from the pane.
- Fixed dropping files from the browser directly into the workspace.
- Documents now support checklist toggles.
- Uploaded file paths now display correctly in references.
- The image picker now accepts every image type it offers.
- Oversized images are resized automatically instead of rejected, and JPEG uploads no longer fail.
- Pinned documents now stay at the top of the pane.
- Pull request artifacts stay in the pane while open and are retired when the pull request ends.

### Code Review and CI
- The automated code reviewer now applies the project's actual development standards.
- The reviewer stops once blocking issues clear instead of restating feedback indefinitely.
- The reviewer checks the exact current code revision instead of a stale patch.
- The reviewer can no longer write to the repository it is reviewing.
- Reviewer findings are now advisory to the author rather than binding.
- Review feedback now states which round it came from, with clearer complexity guidance.
- The reviewer now fails visibly if its rule catalog cannot load.
- Raised the reviewer's turn budget, and its timeouts now identify themselves.
- Reviewer status and resolution results now publish with attributable evidence.

### Providers and Models
- Hidden local models no longer appear in the model picker.
- Fixed a reconnect storm that could silently evict the loaded local model.
- Local-model reasoning capability is now detected directly instead of guessed from the name.
- Context window limits are now derived from each model's actual profile.
- Fixed the model list repeatedly re-scanning models that report no context window.
- Removed an incorrect warning claiming Codex does not support images.
- Fixed several Codex and Claude Code checkpoint bugs that could interrupt in-flight work.
- Model-switch requests are now bounded and no longer trapped behind an in-flight checkpoint.
- Codex usage is attributed to the expected model when the provider does not name one.

### Governance and Access
- Agents can release their own host-access grant instead of waiting for it to expire.
- Host-access lease controls are readable on mobile again.
- A workspace-protection denial now explains why, instead of a generic refusal.
- Access-request reviews now explain why they were denied.
- Fixed a confusing error that described the workspace's own files as forbidden.
- Host-access lease prompts now use the standard responsive drawer on every screen size.
- Raw webhook payload bodies are no longer retained after processing.
- Each install now has its own accounts, with a username and password sign-in and no built-in credential.
- API tokens for agents and scripts can be created, listed, and revoked from the command line.

### Runtime and Operations
- Service start, stop, and restart commands now confirm their actual result.
- Restarts now complete within a bounded time and show progress.
- Fixed cases where a stale Git lock could block startup.
- Redeploys no longer risk running a mismatched backend and frontend pair.
- Fixed a bug where running tests could corrupt the production service's launch configuration.
- Database schema upgrades now run automatically at startup.
- Service stop now reliably reaches a fully stopped state.
- Fixed several startup and restart deadlocks across checkpoint recovery and scheduling.
- Worktree commands can no longer accidentally target the production checkout.
- Drain notifications now reach every relevant listener instead of one fixed recipient.
- Fixed a false failure in the port guard that could block a legitimate checkout.
- Restarts now bind to one exact installed instance, never a stale process.

### Internal
- Renamed "session" to "thread" consistently across the schema, backend, and frontend.
- Consolidated dozens of database write paths onto explicit transaction boundaries.
- Removed a large body of dead code, unreachable paths, and stale internal documentation.
- Split several oversized backend modules into smaller, single-responsibility files.
- Expanded backend test coverage in CI, including a new contract-test suite.
- Moved the fast lint, type, and code-health checks onto hosted runners.
