# Offering a local model

Use this for first-run task 2 when no local model is connected (install skips it when a subscription works), and any time later the person asks for one.

1. Read `corsia_api` `GET /api/providers/local/setup`. If `state` is `running` or `downloading`, setup is already under way (the setup screens start one): make no offer; follow **Setup already running** below. If it is `succeeded`, the model was just set up: make no offer. Otherwise, if `omlx_is_persons_own` is true, the oMLX here is one the person installed, and setup adds no models to it: don't make the offer. Otherwise, if `recommended_profile` is null, this Mac is too small for a recommended model: say nothing and move on. It names the model (`display_name`), its size (`download_size_gb`) and `id`; the Mac's memory picks the tier, never you, and you never name a different model.
2. Ask once, in chat, then end your turn and wait:

> A model that runs on this Mac is for work you'd rather keep local and for background jobs. I'd set up [display_name] ([download_size_gb] GB). It downloads in the background, and the app shows progress. It uses memory while it's loaded. Installing it doesn't move any work by itself; afterwards I can point particular agents or jobs at it if you want. Set it up?

3. Yes: `POST /api/providers/local/setup` with body `{"profile": "<id>"}`. The disk needs room for the rest of the download plus 10%; if it doesn't fit, the flight fails with a message saying how much to free. Report progress truthfully from `GET /api/providers/local/setup` (`state`, `stage`); never promise a speed. On a failure, tell the person what its `message` says and the fix it names. A `400` means the profile is unknown, the Mac has too little memory, or the oMLX is the person's own: don't retry with another profile. If it says it is still downloading, run step 3 again with the same profile (its `next_action`, `corsia local setup --profile <id>`, does the same); never omit the profile, which sets up the default model instead.
4. No: move on. Don't re-offer unless they ask.

## Setup already running

It runs on its own and never holds up a setup task. Post `Your local model is setting up in the background: [stage]. Your agents can use it once it's done.`, with `stage` as it reads (once the download reports, it shows the percent); when `stage` is null, post `Your local model is still downloading in the background. Your agents can use it once it's done.` Then carry on with the rest of task 2.

While setup is open, read the same route again at the end of each later setup task, and whenever the person asks about it (answer with its `stage`). Say nothing while it still runs. The first time it reads `succeeded`, post `Your local model finished setting up. Your agents can use it now.` The first time it reads `failed`, post `Your local model didn't finish setting up: [result.message]`; never post `result.next_action`, a terminal command. Each is said once. If it is still running at the wrap-up, add the clause `The local model is still downloading; the app shows its progress.`
