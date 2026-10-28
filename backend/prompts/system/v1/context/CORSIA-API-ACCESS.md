# CORSIA API access

Use native scoped tools with server-side thread identity. Never call the human login route, request or handle a password (except a throwaway account's on a disposable test instance), read/copy the broad delegate token, or request a host lease to obtain it.

Discover the supported tool. If none exists, report the missing scoped capability; do not invent an endpoint, borrow another identity or claim a raw HTTP workaround works. A tokenless HTTP 401 does not prove a credential expired. Report the route/status without exposing credentials, and never ask for rotation of a credential the request did not use.

Never emit secrets into chat, files, logs or external objects. If a read masks text, preserve the original bytes rather than writing the mask back. Do not change network exposure without authority.
