# Import result lines

After `POST /api/mcp/import`, post one line per returned row, in this order of checks:

- a row with a `message`: that message, as it is.
- `"state": "awaiting_approval"`: `[name]: Waiting for your approval`
- `already_exists`: `[name]: you already have [name] in CORSIA with different settings, so I kept yours.`
- `cannot_connect`: in first run, hand it to step 3's helper like an app that isn't quick, and post `A helper is looking into **[name]**. I'll tell you here what it finds.` At any other time, post `**[name]**: there's no ready way to connect it yet. It's on my list.` and add your own task to build it, as skill `connect-tools` says under "No connection exists yet".
- `failed`: `[name]: I couldn't bring it over.`

Then post `Approve them in [Settings › Connected apps](/settings/connected-apps).` Nothing an import brings over works until the person approves it there.
