# A new install's first run: the app path

The setup screens already did the work: one model source, the account and the settings are done and shown. The server has posted the opening and seeded the structured plan with the steps that apply, before any model call. Never write the opening or create the setup plan. Work the existing steps in order; mark each done when complete, or close it with the person's reason when skipped. Recheck a step's skip condition when you reach it, since an earlier step may have done its work. On "skip the rest", close the remaining setup steps and the step 3 asking child, leave `Connect [app]` tasks open, and go to the hand-off. Close the step 3 asking child before its phase; if app children remain open, keep their phase open until they settle. The tasks steps 3 and 6 make are follow-up work, so do add those. `references/first-run.md` is the script path; its task list and ordering do not apply here. Its **Never** rules (after "What the person sees") apply here too; its "Host access, explained once" does not (step 8 is the app-path form).

**Facts to read from `## Setup facts`**, with no other read before the person says yes and none of another app's settings: `harness_ready` (the ready ones of `claude_code`, `codex`), `sources_missing` (each not-ready `claude_code` or `codex`, with `not_installed`, `needs_auth`, `unusable` or `limited`), `tool_attempts` (what setup tried for each of `claude_code`, `codex`: `none`, `installed`, `install_failed` or `signin_failed`, a failure with its `cause` code, which you always put in plain words and never post as it is), `pays_for` (the ones of `claude_code`, `codex` the person said on the setup screen about subscriptions that they pay for, an empty list when they said neither or were not asked; a tool it doesn't list is never offered), `agents_found` (eligible tools among `claude-code`, `codex`, `openclaw` and `hermes`, detected on this machine by CORSIA itself (a stat check, and for claude-code whether it has synced any of the person's own claude.ai skills); nothing from their settings, and nothing for you to read), `local_download` (its `state`, and the model, cause or progress that state carries), `auto_approve` (Auto-approve is on: true or false; an `unavailable` mark means say nothing about its state), `local_eligible` (this computer can run a local model; any other value, an `unavailable` mark included, means say nothing about local models), `web_search_ready` (one real search works; every install can search without a key, and a Brave key can make it steadier), `web_search_key` (a Brave key is saved, so searches go to Brave), `github_ready` (the agents' GitHub account is ready or the person chose how agents post), `user_profile` (read from the workspace `context/50-USER-PROFILE.md`: `name` is its Preferred name line and `full_name` its Full name line, null where the line is missing), `bound_model` (the model the person picked in the first chat, and its source), `connected_app_count` (the number of connected apps), `email_calendar_ready` (a connected account has both mail and calendar), and the decisions in `setup.setup_decisions` that the steps below name. Never restate the source set up on the screens, or a failure the screens showed. A fact marked `unavailable` names its read.

**How you talk.** Send every line the person must read with `post_update`, one step at a time: post it, end your turn, and wait for their answer, which wakes you. Each step says what it is and why, asks once, and accepts "not now" (carry on without chasing; never move it to a later task). A step that does not apply is skipped without a word, never postponed. You do the work; the person only chooses, signs in or types their own password.

**"Skip the rest"**, at any point: post `Done for now. Anything I skipped, ask me any time and I'll set it up.` then go straight to the hand-off. Never ask about a skipped step again in this conversation. The silent checks before the hand-off still run.

**The local model's news.** Never read the local download yourself. When it finishes or fails after setup, a wake tells you (`onboarding.local_download`), at most once per outcome. Post the news first, as its own message, word for word as below; it never asks anything. Then, when the wake came with the person's answer, handle that answer as usual; when your question is still unanswered, end your turn: that question stays open. If `local_download` in your Setup facts already shows that same outcome, say nothing. The wake can arrive after the hand-off; post it the same way. Per outcome:
- `ready`: `Your local model, [model], is ready. It runs on this computer, so it's private and free to use. Pick it in the model picker any time.`
- `failed`, `setup_failed` or `start_failed`: in one message, say in plain words what went wrong (from its `cause`; never the code itself) and pass on its `fix` in plain words, plus `ref` when present. Nothing more.

## The opening

The server posts `references/first-run-opening.md` as your first message, with `Hi [name]. ` only when `user_profile` has a name. Never post or repeat it. Your first generated message is the first applicable setup step; the inline kickoff carries that step.

## The steps, in this order

Each step is in its own file. Read a step's file when you start that step, even if you read it earlier: an earlier read may no longer be in front of you. Its task in your plan names the file.

1. Add your Claude or ChatGPT subscription: `references/first-run-app-1-subscription.md`
2. Bring over what your other tools know: `references/first-run-app-2-import.md`
3. Where your work lives: `references/first-run-app-3-apps.md`
4. Web search: `references/first-run-app-4-web-search.md`
5. About you: `references/first-run-app-5-about-you.md`
6. Email and calendar: `references/first-run-app-6-email-calendar.md`
7. How CORSIA gets better: `references/first-run-app-7-community.md`
8. Your files, and when agents ask first: `references/first-run-app-8-files.md`
9. Hand over to your agents: `references/first-run-app-9-hand-over.md`
