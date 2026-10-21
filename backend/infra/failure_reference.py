"""Public, non-secret references that correlate user-visible failures with logs."""

import secrets


def new_reference(prefix: str = "cli") -> str:
    """Mint an opaque reference, labelled for the operation that failed."""
    return f"{prefix}-{secrets.token_hex(8)}"
