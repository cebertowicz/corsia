---
name: assistant-onboarding
description: Set up or operate an Assistant for email, calendar, Drive and Docs after a user selects the first-run Assistant option. Use for Google Workspace setup, safe email drafts or sends, Docs safe writes, and scheduled inbox checks. Not for creating a general project or for silently sending, overwriting, accepting invites, or sharing files.
corsia:
  handler:
    kind: instruction
---

# Assistant onboarding

Show the whole plan before acting. The Assistant owns its Google credentials: the conductor creates and starts the Assistant first and runs no Google, `gws`, `gws-env.sh`, `agent-browser` or Homebrew step itself, because credentials set up in the conductor's thread would land in the conductor's own Google Workspace home, which the Assistant's shell and scheduled checks cannot read. The Assistant runs the setup in its own thread through its attached `assistant-google-setup` skill. The user approves one install if the assistant's tools are missing, signs in to Google and clicks Allow; the Assistant performs every other setup action. Do not read credentials, print them, or call an external service until the user reaches the relevant setup step.

## Offer it only for a Gmail address

Before offering the Google Assistant, check the person's email, the one they chose at install. Offer it only for a `gmail.com` or `googlemail.com` address (a Google account on another domain is not assumed). For any other address do not offer it: ask what the first agent should own instead, and explain why an agent that connects directly to your accounts beats a third-party app: it reaches the service itself, no company sits in between, and you own the connection.

## Explain it first

Before creating anything, the conductor posts this with `post_update`:

> Your Assistant is an agent that looks after your email, calendar, Drive and Google Docs, every day, not just when you ask. It reads new mail on a schedule you choose, tells you what needs you, drafts replies for you to check, and keeps your calendar and documents straight. It never sends, accepts an invite, shares a file or overwrites a document unless you've said it may.
>
> What it needs: your Google account, connected once, and no third-party app in between. Your Assistant builds its own private Google app inside your Google account: it creates a Google Cloud project, turns on Gmail, Calendar, Drive and Docs, sets up the sign-in screen and makes the app's credentials, all in a browser you can watch. The app belongs to you, only this Mac uses it, and Google doesn't charge for it. You sign in and click Allow; it does everything else. Then it asks you a few questions so it works the way you do.

## Create and start the Assistant

Only the CORSIA agent performs this sequence, and it comes first. `coordination-api` is canonical for API constraints, error handling, timeouts, and retry safety; do not restate or bypass it.

1. Read the `Create your Assistant` plan item using `<assistant_setup_node_id>`. If its description already contains `assistant_thread_id=<thread_id>`, resume that setup instead of creating another Assistant. Otherwise read `model_catalog`, select the conductor's exact ready model and account under model policy, then call `delegate_create_thread(title="Assistant", model=<selected model>, account_id=<selected account id>, thread_kind="chat")`. Keep the returned `thread_id` and immediately call `plan_update` with top-level `plan_id`, a unique `mutation_id`, `operation="edit"`, and `params={node_id:<assistant_setup_node_id>, expected_revision:<assistant_setup_node_revision>, description:"assistant_thread_id=<thread_id>"}`. Keep the returned node's new revision for later edits.
2. Call `agent_roster_entry(thread_id=<thread_id>)`, then call `agent_roster_update(thread_id=<thread_id>, expected_updated_at=<entry updated_at>, agent_enabled=true, responsibility="Email, calendar, Drive, and Docs assistance", responsibility_paths=[], responsibility_domains=["email","calendar","drive","docs"], coordination_notes="Created through Assistant onboarding.")`. Then call `agent_roster_entry(thread_id=<thread_id>)` again and confirm `agent_enabled: true` before anything else; if it is not, repeat the update once with the fresh `updated_at`, and if it is still not, tell the user the Assistant could not be enabled and stop.
3. Load `coordination_instruction_file_create`. Read `.corsia/skills/assistant-onboarding/references/assistant-responsibility.md`, then create and attach it with `target_thread_id=<thread_id>`, `kind="responsibility"`, `expected_revision="absent"`, and its complete text as `content`. Require `delivery.next_turn=true`.
4. Attach all four distinct skills with `kind="skill"`, `expected_revision="absent"`, their exact `name`, and complete text: `assistant-google-setup` from `references/assistant-google-setup-skill.md`; `assistant-email-check` from `references/assistant-email-check-skill.md`; `assistant-google-workspace` from `references/assistant-google-workspace-skill.md`; and `docs-safe-write` from `references/docs-safe-write-skill.md`. Require `delivery.next_turn=true` for each.
5. Use `delegate_send` once to tell `<thread_id>`: `Run your Google setup now, then create or reuse your scheduled email check and confirm its schedule id to the user.` The target lists before creating on every such kickoff. Do not wait for, retry, or relay confirmation; do not poll or create its schedule yourself, and run no Google step. Tell the user that their Assistant will walk them through Google sign-in in [the Assistant chat](/thread/<thread_id>) and confirm the schedule there; always hand them to the conversation with that link.
6. Leave `Set up Google tools`, `Connect your Google account` and `Show your first result` to the Assistant's chat: the Assistant posts the linked, read-only first result itself. Close those items only when the Assistant's update or the user confirms them.

## Email and Docs safety

- The attached `assistant-google-workspace` skill writes every reply as an editable CORSIA document with recipients, subject, and body. Drafting is not approval.
- Invoke `.corsia/skills/assistant-onboarding/assets/gws-reply-guard.py` only when the user explicitly says `send` or `send it` in chat. Re-read the document immediately before that exact call and send its current text only.
- The attached `docs-safe-write` skill reads a Docs revision before every write and passes that revision as `writeControl.requiredRevisionId`. On rejection, it re-reads and re-applies only when safe; otherwise it shows the conflict and asks.

## Scheduled email check

The attached `assistant-email-check` skill is the Assistant's sole scheduling instruction. It lists the Assistant's schedules before creating one, so exactly one active bounded cadence schedule with payload `{ "kind": "assistant.scheduled_email_check" }` exists: `spec: "3600"`, `max_runs: 1000`. Which wakes do a real check follows the person's answer to the Assistant's `How often should I check your email?` (hourly on weekdays 08:00–18:00, twice a day, or a morning digest). It confirms the schedule id to the user with `post_update`. The conductor sends one kickoff only and never creates the Assistant’s schedule itself.

Each cadence wake is model-visible. Before any Google call or update, check the saved window. Outside it, skip the service call and update, but never claim this costs zero model turns or tokens. Report guard-wake and real-check model/token cost separately. Do not add a loop or poll.
