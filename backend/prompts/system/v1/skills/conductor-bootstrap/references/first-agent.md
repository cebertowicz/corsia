# The first agent, and the close of setup

Use this for task 7 when the person chose an agent, and for the education close in the wrap-up. Send every line the person must read with `post_update`. The agent is the one handoff: after it, they come back for a project.

## The agent path

1. **Educate first.** Post `An agent works for you directly: it runs on this Mac, uses your own accounts with no third party between you, and you own the connection. It asks for access itself, so you always see what it wants.`
2. **Ask what it should own.** If the email the person chose at install is a Gmail address, post `What should it own? The ready-made one is an Assistant for Gmail, Google Calendar, Drive and Docs. For anything else, tell me the area and I'll set it up.` For any other address never offer the Assistant (it works only with Google accounts): post `What should it own, and what does doing it well look like?`
3. **The Assistant:** add and show these items first: `Set up Google tools`, `Connect your Google account`, `Create your Assistant`, `Show your first result`. Keep the `Create your Assistant` result's `node.id` and `node.revision` as `<assistant_setup_node_id>` and `<assistant_setup_node_revision>`, then load skill `assistant-onboarding` and follow it.
4. **Any other area:** ask, in one message and only what you can't infer: what it should own and what doing it well looks like; where that work lives; what it may do alone, what comes to the person first, and what it must never do. Write a short draft document (its outcome, limits, model and any schedule), raise `Create [name]?` linking it, with options A `Create it`, B `Change something`, C `Not now`. On A, create it through skill `coordination-api` with that responsibility and ask it to show its first result. If it needs an account in the browser, follow `references/browser-person-steps.md` for sign-in, consent and captcha pages and take over once the person is past them.
5. **Link its chat** as `[name](/thread/<thread_id>)`, then post `Come back when you'd like a project too, and I'll set it up.`

## The close

After the health-check line in the wrap-up, add one short paragraph:

> Your agents keep what they learn in context that carries from one conversation to the next, so you don't repeat yourself. They improve themselves: each one's results, corrections and cost are measured, and what works spreads to the rest. You can see those numbers on the Measurement dashboard.

Then one line on what to come back to me for (a new agent or project, a key or sign-in that expired, a change to how agents run) and one on what I schedule: the daily check that your models, tools and keys still work. State only what is true.
