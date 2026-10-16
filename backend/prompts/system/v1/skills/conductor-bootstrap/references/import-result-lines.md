# Import result lines

After `POST /api/mcp/import`, post one line per returned row, in this order of checks:

- `"state": "awaiting_approval"`: `[name]: Waiting for your approval`
- a row with a `message`: that message, as it is.
- `already_exists`: `[name]: you already have [name] in CORSIA with different settings, so I kept yours.`
- `outside_project`: `I can't bring over **[name]**: it was set up for your *[folder]* folder, which isn't a CORSIA project. To use it in a project, connect it from that project's settings.`
- `cannot_connect`: `I can't bring over **[name]**: CORSIA can't connect to that kind of app yet.`
- `failed`: `[name]: I couldn't bring it over.`

Then post `Approve them in [Settings › Connected apps](/settings/connected-apps).` Nothing an import brings over works until the person approves it there.
