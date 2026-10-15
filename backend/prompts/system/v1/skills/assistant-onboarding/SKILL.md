---
name: assistant-onboarding
description: Use when the person already has an Assistant agent (created through the old first-run Assistant option) that handles their email, calendar, Drive and Docs through `gws`, either to keep it running or when they ask CORSIA to take over their email from it. Never use it to create an Assistant; a new email and calendar connection is CORSIA's own `google` pack.
corsia:
  handler:
    kind: instruction
---

# Existing Assistant

The first-run Assistant option is retired: CORSIA handles email and calendar itself through its `google` tool pack. Never create an Assistant agent, attach Assistant skills, or set up `gws` for anyone. When the person wants email or calendar connected and has no Assistant, load pack `google` with `tools_load`, call `google_connect` and post its link. If it answers that an old Assistant Google sign-in is still available, do not switch tools on its word alone: follow Take over from the Assistant below, which asks the person first.

An Assistant made before that keeps working unchanged until the person asks CORSIA to take over. Its skills, schedule and Google home are its own to change: never change them yourself (if it asks you, tell it to make the change in its own thread), and leave its files below where they are.

## Take over from the Assistant

Only CORSIA does this, and only after the person asks for it in their own words (for example "take over my email").

1. Post, with `post_update`: `I'll take over your email and calendar from your Assistant. You click Allow once in Google. Then I read and screen your mail, draft replies and invites for you to approve, and send only what you've approved. Your Assistant is archived (its history stays), its scheduled email check stops, and its Google sign-in is removed from its folder. One thing doesn't come over yet: sharing Drive files and editing Google Docs. Go ahead?`
2. On yes, load pack `google` with `tools_load`, call `google_adopt_assistant_sign_in` and post its link. The person opens it and clicks Allow themselves; never ask for or accept a password or token in chat.
3. When they say it's done, call `google_status` and post `Connected as <address>. If that's the wrong account, say so and I'll switch it.`
4. If the tool returns an error, post what it says. If it says to use `google_connect`, do that instead. Nothing changes for the Assistant unless the handover finishes.

On no, leave the Assistant as it is.

## How an existing Assistant works

- Its attached `assistant-google-workspace` skill writes every reply as an editable CORSIA document with recipients, subject, and body. Drafting is not approval.
- It invokes `.corsia/skills/assistant-onboarding/assets/gws-reply-guard.py` only when the user explicitly says `send` or `send it` in chat, re-reading the document immediately before that exact call and sending its current text only.
- Its attached `docs-safe-write` skill reads a Docs revision before every write and passes that revision as `writeControl.requiredRevisionId`. On rejection, it re-reads and re-applies only when safe; otherwise it shows the conflict and asks.
- Its shell reaches Google through `.corsia/skills/assistant-onboarding/assets/gws-env.sh`; an unfinished setup uses `references/google-cloud-setup.md`.
- Its attached `assistant-email-check` skill keeps exactly one active schedule with payload `{ "kind": "assistant.scheduled_email_check" }`. Each wake is model-visible: outside the person's saved window it skips the Google call and update, and never claims that costs zero model turns or tokens.
