# CORSIA client metadata document

`corsia-client.json` is the small public file an OAuth provider fetches to learn what CORSIA is when a person signs in to a hosted connected app (the "client ID metadata document" the newest MCP authorization spec prefers). The file's own HTTPS address is CORSIA's client ID at every provider.

## Status

Off by default. While `CORSIA_CLIENT_METADATA_URL` (`backend/infra/config.py`) is empty, CORSIA signs in with automatic registration or a pasted client ID. Once it is set, CORSIA uses it with providers that accept metadata documents (`backend/tools/mcp_oauth_protocol.py`).

## Turning it on

1. Publish `corsia-client.json` at a fixed HTTPS address (planned: the project owner's domain through GitHub Pages).
2. Replace `client_id` in the file with that exact address, then publish the edited file.
3. Set `CORSIA_CLIENT_METADATA_URL` to the same address.

## Never move it

Providers key refresh tokens to the client ID. Changing the address signs every person out of every provider. Keep the address and the file's `client_id` identical forever.

## What it says

- A native app (`application_type`), no client secret (`token_endpoint_auth_method: none`), PKCE sign-in with the authorization-code and refresh-token grants.
- Loopback callbacks only: `127.0.0.1`, `localhost` and `[::1]` at `/callback`. The port is chosen at sign-in time; providers match loopback addresses on any port (RFC 8252 §7.3).

`backend/tests/test_client_metadata_document.py` checks the file's shape.

Checked against the code at `7a8c55d84`.
