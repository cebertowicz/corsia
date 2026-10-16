# Offering a local model

Use this for first-run task 2 when no local model is connected (install skips it when a subscription works), and any time later the person asks for one.

1. Read `corsia_api` `GET /api/providers/local/setup`. If `state` is `running` or `downloading`, setup is already under way (the setup screens start one): make no offer; follow **Setup already running** below. If it is `succeeded`, re-check with `model_sources` `recheck: true`: if a local model is connected, it was just set up, so make no offer; if none is, that result is old: carry on as if it were absent. Otherwise, if `omlx_is_persons_own` is true, the oMLX here is one the person installed, and setup adds no models to it: don't make the offer. Otherwise read `local_model`. If its `state` is `low_memory`, `none_fits` or `own_omlx`, setup has no model for this Mac: say nothing and move on. If it is `picked`, its `model` names the model (`name`) and its size (`download_size_gb`). If it is `not_checked`, `checking` or `failed`, the model is unknown until setup asks: make the offer without a name or size. oMLX picks the model for this Mac, never you; never name another.
2. Ask once, in chat, then end your turn and wait:

> A model that runs on this Mac is for work you'd rather keep local and for background jobs. I'd set up [name] ([download_size_gb] GB). It downloads in the background, and the app shows progress. It uses memory while it's loaded. Installing it doesn't move any work by itself; afterwards I can point particular agents or jobs at it if you want. Set it up?

With no model picked, say `the model that suits this Mac best` in place of `[name] ([download_size_gb] GB)`.

3. Yes: `POST /api/providers/local/setup` with body `{}`; setup installs the model oMLX picks. The disk needs room for the rest of the download plus 10%; if it doesn't fit, the flight fails with a message saying how much to free. Report progress truthfully from `GET /api/providers/local/setup` (`state`, `stage`); never promise a speed. On a failure, tell the person what its `message` says and the fix it names. A result with `skipped` `none_fits` means no model fits this Mac: tell the person its `message` and stop. A `409` with `detail.code` `consent_required` means CORSIA found an existing oMLX and needs to read its version, settings, and local server only to adopt it without changing its settings: explain that, ask the person, and only after a yes resend the same request with `"consent_existing": true`; on a no, stop. If it says it is still downloading, run step 3 again (its `next_action`, `corsia local setup`, does the same).
4. No: move on. Don't re-offer unless they ask.

## Setup already running

It runs on its own and never holds up a setup task. Post `Your local model is setting up in the background: [stage]. Your agents can use it once it's done.`, with `stage` as it reads (once the download reports, it shows the percent); when `stage` is null, post `Your local model is still downloading in the background. Your agents can use it once it's done.` Then carry on with the rest of task 2.

While setup is open, read the same route again at the end of each later setup task, and whenever the person asks about it (answer with its `stage`). Say nothing while it still runs. The first time it reads `succeeded`, post `Your local model finished setting up. Your agents can use it now.` The first time it reads `failed`, post `Your local model didn't finish setting up: [result.message]`; never post `result.next_action`, a terminal command. Each is said once. If it is still running at the wrap-up, add the clause `The local model is still downloading; the app shows its progress.`
