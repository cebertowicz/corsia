# Import result lines

After `POST /api/mcp/import`, post one line per returned row, in this order of checks:

- a row with a `message`: that message, as it is.
- `"state": "awaiting_approval"`: `[name]: Waiting for your approval`
- `already_exists`: `[name]: you already have [name] in CORSIA with different settings, so I kept yours.`
- `cannot_connect`: `I can't bring over **[name]**: CORSIA can't connect to that kind of app yet.`
- `failed`: `[name]: I couldn't bring it over.`

Then post `Approve them in [Settings › Connected apps](/settings/connected-apps).` Nothing an import brings over works until the person approves it there.
