# CORSIA copy standard

**Status:** current · **Owner:** CORSIA maintainers

## Scope

Every string CORSIA's interface shows, in every state: labels, buttons, hints, empty states, toasts, errors and system messages. It doesn't cover what agents write in chat, or agent-facing text (prompts, tool schemas and results, `backend/prompts/`), which deliberately uses internal words. The PR reviewer Flags frontend strings and backend strings on a known user-facing path (HTTP error detail, notification text). For any other backend string it asks whether the string reaches a screen rather than Flagging.

## Reviewer Contract

### Rules

| # | Rule | How a reviewer tests it | Boundary | Checked at |
|---|---|---|---|---|
| C1 | **Length fits the surface.** | Read it in its real slot at 1920 and at 393. Meaning that hides behind an ellipsis fails. A button is a verb of 1–3 words; a row's second line is one line; a toast is one sentence; an error is one sentence plus its next step. | An error may be longer to be complete (Microsoft); extra detail goes behind Details. Translated text can run 2–3× longer, so layouts must allow growth (W3C). | Both (reviewer: word counts and sentence limits; truncation is a Nit, "check in render") |
| C2 | **Written for someone outside the team.** No internal decisions, mechanism or roster names, and no informal tone ("Oops", jokes, "!"). Product chrome never speaks in the first person. | Would a capable professional who has never seen our code or plans follow it? | Agents speak in chat; product chrome doesn't. The Debug page is exempt from C2, C4 and C5, but not from the error rules. | Both |
| C3 | **One reasonable reading.** | Name what it means and what happens next. If there are two readings, it fails. For any string longer than a line, a fresh reader given only the screen must say the same. | Clarity beats technical precision (NN/g). | Both (reviewer: the string itself has two readings; the fresh-reader test on the screen is design review's) |
| C4 | **One word per concept, from the glossary.** No term a person wouldn't already know. | Every noun naming a CORSIA concept is in the glossary below. A new one is added in the same change, with why it's widely understood. | Same-function components use the same name (WCAG 3.2.4). Product names (Claude Code, oMLX) are fine. | Both |
| C5 | **Reads naturally.** Full sentences with their articles; no noun stacks. | Read it aloud. Does it sound like something a person would say (GOV.UK, Apple)? | The reader is a capable non-developer professional. Don't write down to them (digital.gov). No reading-grade formula: none is validated for interface text. | Both |
| C6 | **Say the common case; don't over-explain.** An edge case shows only in the state where it happens. | Delete each clause. If nothing is lost for the common case, it goes. | Something that can't be undone, and data loss, are stated once, at the confirmation (Apple, NN/g). That isn't over-explaining. A hint on how to use the surface is UX21, not C6. | Both (reviewer: a deletable clause; whether an edge case shows only in its own state is design review's) |
| C7 | **Nothing temporary.** No "yet", "now", "currently", "for now", "temporarily", "new" or "soon". | Will it still be true after the next release, with nobody editing it? | Live state that updates itself ("Loading…", "Waiting for you in your browser") and dated text (release notes) are fine (Google). "New" as an action that creates something ("New chat", "New code", "New project") is fine; it is banned only as a claim about age ("new feature", "New!"). | Both |
| C8 | **No history and no counterfactuals.** State what is true now, not what changed, moved, was removed or used to be. | Read it as someone who never saw the old version. Does it still make sense? | A confirmation may state what the action will do ("Agents won't be able to use Figma"). Even a change during the person's session is written as the current state. | Both |
| C9 | **Every error says what happened and what to do.** If the person can't act, it says what CORSIA is doing about it (retrying, kept your draft, will try again at 3:00). | Who acts next, and how? If the answer is "nobody", the failure isn't handled, and rewording won't fix it. | Problems the person can't fix are shown as a state with the next step, not red error text (GOV.UK). Input errors name the field in its own words (WCAG 3.3.1, 3.3.3). | Both |
| C10 | **Errors are traceable.** A system failure shows a short reference the person can quote, and that reference finds the log entry. | Search `backend.log` on that Mac for the reference shown. The log stays on the person's machine, so the reference serves an agent there, or a maintainer the person sends the log to. | Input errors need none. A code is never the message (Apple, NN/g, GOV.UK); it sits beside it, as its own field. Applies once error references exist (see Severity). | Both (design review checks that the error state shows the reference; the reviewer checks that the code sends it) |
| C11 | **Errors name the cause, not the symptom.** One message per known cause. Raw exception text, an HTTP status or "Internal Server Error" is never the message. | Does it say why, in the person's terms? Is the generic text used only when the cause really is unknown? | Unknown causes get the generic sentence plus the reference (Microsoft: a generic message only when the cause can't be determined). | Both |

| # | Rule | Checked at |
|---|---|---|
| C12 | Buttons are verbs naming the outcome. Delete means gone for good, Remove means detached but kept, Archive means hidden but kept. Cancel only cancels. | Both |
| C13 | A confirmation names the object and the consequence, never just "Are you sure?" | Both |
| C14 | No blame or filler words: invalid, illegal, wrong, bad, please, sorry, oops, and no exclamation marks. | Both |
| C15 | Sentence case everywhere. One failure phrase, "Couldn't", with a curly apostrophe. | Both |
| C16 | An error appears where the problem is, in the control's own words. | Design review (reviewer: Nit, "check in render") |
| C17 | No meaning lives in text that truncates. Layouts allow 2–3× growth for translation. | Design review (reviewer: Nit, "check in render") |

### Checked at

**Checked at** says which gate applies a rule. Each gate applies every rule it can judge, at the same severity.

- **Design review** reads every string in every state, rendered at 1920 and at 393.
- **The PR reviewer** reads a diff and renders nothing. It applies every rule it can judge from the text alone. For C3 and C6 that is the text-only half. For C3, the string itself has two readings. For C6, a clause can be deleted without losing anything in the common case. The other half of each, the fresh reader on the real screen for C3 and which state shows the string for C6, stays with gate 1, just as the layout half of C1, C16 and C17 does. For those layout halves it raises a Nit ("check in render"), because only a rendered screen can show them. It cites the rule number (for example C7) as each finding's section, so findings can be counted per rule.
- **Demotion bar:** after the first 20 PRs, a rule whose gate-2 Flags fall below 80% precision, over at least 10 audited findings, drops to a Nit at gate 2. It stays a Flag at gate 1. Quality Gate reports against this bar, so the audit settles it mechanically.

### Severity

- **Block:**
  - an error with no next step for the person or CORSIA (C9)
  - raw runtime, exception or HTTP text, or a code, as the message (C11)
  - once references exist, a system failure without one (C10)
- **Flag:** any other rule broken in copy the change adds or edits, on any surface, raised only by a review stage that can apply that rule (see Checked at). Like every Flag today, it is fixed or dispositioned before merge; it doesn't fail the reviewer status. There is no wording or copy-tone Nit.
- **Nit:** typography that doesn't change the reading, such as an apostrophe style.
- **Untouched copy** on the same screen is listed in the review comment, not counted against the change and never filed automatically. The owning lane batches what it receives into its sweep.

### Glossary

| Use | Not |
|---|---|
| **chat** | thread, conversation |
| **project**, **workspace** | scope |
| **file** | artifact |
| **document** | — |
| **Files** (the side panel) | artifact panel |
| **agent** | worker, subagent, seat, harness |
| **skill** | — |
| **task**, **plan** | step, node, item |
| **decision** | ask (as a noun), item, question |
| **review** | ask (as a noun), item |
| **question** | ask |
| **model**, **provider**, **account**, **subscription** | harness, plan (for a subscription) |
| **connected app** (or the app's own name, "GitHub"), **action** | MCP server, tool server, server, tool |
| **sign in**, **sign out** | log in, log out, token |
| **checkpoint** | compaction, fold |
| **Retry** (button), **Try again** (sentence) | Reload, except for a page reload |
| **CORSIA** | Corsia, we |
| **collapse**, **hide**, **group** | fold |
| **this computer** (the person's machine); **Mac** only for an Apple fact (Apple silicon, the macOS version, System Settings) | this Mac, this machine |
| **Local only** (a chat setting), **Local models only** (a project setting) | offline, private mode, cloud off |
| **Trusted device** (a network model server's switch) | private machine, private server |
| **main computer** (the computer other devices connect to) | host, server, home computer |
| **outside address** (the main computer's address for devices away from its network) | public address, external address, remote address, endpoint, domain |
