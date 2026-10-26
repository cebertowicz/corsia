# The About you doc

For the CORSIA agent: step 5 of `references/first-run-app.md` sends you here once the person has answered the questions in `references/look-up-the-person.md`. Do the sections in order. Post only the lines in backticks, word for word. Skip this whole reference when they answered none of the questions.

## 1. Make the doc

Create the registered workspace document `shared/about-you.md`, outside `.threads/`, so the person can open it, with this and nothing else, no text under a heading:

```
# About you

## Who you are

## What you're working toward

## How you like to work
```

Open it in the doc pane with `document_open`. When it cannot open, post the lines you would have suggested under those three headings in chat and ask `Keep these?`. On a yes, write the profile from them (section 5). On a no, from their chat answers.

## 2. Fill it in with suggestions

Every line goes in as a suggestion the person can accept, never as text you wrote into the doc. A suggestion is `doc_comment_create` with `quote` the heading line exactly and `replacement` that heading line followed by its lines. A suggestion never touches another heading's section, so no two overlap. Write only what the person said or didn't correct, what the lookup kept and what the import showed. Never run a web search for it.

What each heading gets:

- **Who you are:** the lookup's kept lines and their answer to question 1.
- **What you're working toward:** their answer to question 2 and the matches step G of the lookup held (what they connected, imported or have on this computer).
- **How you like to work:** their answer to the answer-style question and the working-style notes the import kept.

A heading with nothing to put under it gets no suggestion.

**On a cloud model** (`bound_model` `source` is not `omlx`): start one helper per heading that has material, all at once. First add one `About you: <heading>` task to your plan per helper, then call `delegate_task` for each with `model` the one you run on and that task's id as `plan_node_id`. If `delegate_task` refuses the model, keep that task as your own: write that heading yourself, then `mark_done` the task. Each brief is self-contained: the document's path, its one heading, only that heading's material above, the suggestion form above, and that it suggests inside its own heading only, adds nothing the person didn't say and the lookup and import didn't show, runs no web search and asks the person nothing. A helper with nothing to add writes nothing. A helper that returns puts its `About you: <heading>` task up for your review: `accept` it once that heading's suggestion is in. A helper that fails or returns nothing: write that heading's suggestion yourself, then settle its task: `accept` it if it is awaiting your review, else `mark_done`.

**On the local model** (`bound_model` `source` is `omlx`): start no helper. Write the suggestions yourself, one heading at a time.

## 3. Ask for the review, once

When every `About you: <heading>` task is settled and every suggestion is in, add the task `Review your About you doc` to your plan and hold it on the person: `plan_update` `block_node` with `owner_kind` `user`, `reason_key` `review_about_you`, `ask_artifact_id` the document's artifact id (`artifact_resolve` on `shared/about-you.md`) and `reason` `Change anything that's wrong, reject any suggestion that isn't right, or add a note, then press Approve.` That ask is what puts Approve in the document; without it the person sees no button. Then post once `I put what I know in your About you doc. Every agent and chat here reads it once you approve. Change anything that's wrong, reject any suggestion that isn't right, or add a note on a line, then press Approve. It accepts the rest for you.`

- Whenever you read the document (on Approve in section 4, or on the person's reply), suggest again yourself, on the text now in the doc, any suggestion that can no longer apply to it. Never wait on a helper that is still running.
- A comment from the person on a line: answer with a suggested change to that line.
- "Not now", or they carry on with something else: close that task with the reason that they chose not now, skip to section 5 and write the profile from the chat answers.

## 4. When they approve

First, when any suggestion is still open: accept nothing, remove nothing and write no profile. Post `[N] of my suggestions are still open, so nothing's saved yet. Accept the ones that are right, reject the rest, then tell me here when you're done.` with N the number open. When they reply, read the document again: while any is still open, post the same line with the new count; when none is open, go on. With none open: accept nothing yourself. Read the document's final text. Remove any heading left with nothing under it, so no placeholder ever stays in their document. Then post `Saved. Every agent and chat now works from this. Edit it any time in Settings > Context; I'll keep it current.` and go on to step 6 of `references/first-run-app.md`, after writing the profile (section 5).

## 5. Write the profile

From the document's final text (or, after "Not now" or a no above, from their chat answers), write what `references/look-up-the-person.md` sections E and F name, in the formats they give: `USER.md` for who they are (work, place and background), and the lines in `context/50-USER-PROFILE.md` for their time zone, the name they go by and how they like to work. Write what the text says and nothing more.
