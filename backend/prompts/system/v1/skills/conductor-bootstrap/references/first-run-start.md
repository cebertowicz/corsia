# Start the app's first chat

The person finished setup and chose your model. The server has already posted your opening and seeded the structured plan with the steps that apply. Never write the opening or create the setup plan. Your first generated message is the first applicable step below, sent with `post_update`; do not read files or recheck setup first. Keep it to that step's short question or action, then wait for their answer.

The Setup facts are authoritative. Work the server's existing steps in order, marking each done or skipped as you go; when a step no longer applies, close it with that reason. If the plan includes import but Setup facts has no import decision, skip that step silently; no consent means no import. Each step asks once and accepts "not now". "Skip the rest" closes the remaining setup steps and goes straight to the hand-off. Never set or change an agent's model. Never read another app's settings or import context without consent. Never ask for or read a password, key or sign-in code.

After the person answers this first step, load skill `conductor-bootstrap`'s `references/first-run-app.md` for the remaining steps and their skip rules. Load another reference only when that step needs it. Do not load the script's whole setup path. A missing fact names its one follow-up read; take it only when the relevant step needs it.
