# The first agent, and the close of setup

Use this for task 7 when the person chose an agent, and for the education close in the wrap-up. Send every line the person must read with `post_update`. The agent is the one handoff: after it, they come back for a project.

## The agent path

1. **Educate first.** Post `An agent works for you directly: it runs on this Mac, uses your own accounts with no third party between you, and you own the connection. It asks for access itself, so you always see what it wants.`
2. **Ask what it should own.** Post `What should it own, and what does doing it well look like?` Email and calendar are yours, not an agent's: if that's the area and `gmail_eligible`, connect Google with skill `google-setup` instead of creating an agent; otherwise say it works only with a Google account.
3. **Shape it:** ask, in one message and only what you can't infer: what it should own and what doing it well looks like; where that work lives; what it may do alone, what comes to the person first, and what it must never do. Write a short draft document (its outcome, limits, model and any schedule), raise `Create [name]?` linking it, with options A `Create it`, B `Change something`, C `Not now`. On A, create it through skill `coordination-api` with that responsibility and ask it to show its first result. If it needs an account in the browser, follow `references/browser-person-steps.md` for sign-in, consent and captcha pages and take over once the person is past them.
4. **Link its chat** as `[name](/thread/<thread_id>)`, then post `Come back when you'd like a project too, and I'll set it up.`

## The close

After the health-check line in the wrap-up, add one short paragraph:

> Your agents keep what they learn in context that carries from one conversation to the next, so you don't repeat yourself. They improve themselves: each one's results, corrections and cost are measured, and what works spreads to the rest. You can see those numbers on the Measurement dashboard.

Then one line on what to come back to me for (a new agent or project, a key or sign-in that expired, a change to how agents run) and one on what I schedule: the daily check that your models, tools and keys still work. State only what is true.
