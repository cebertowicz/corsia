"""Opaque references for correlating API failures with server logs."""

import secrets

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_error_reference() -> str:
    """Mint a six-character uppercase Crockford base32 reference."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(6))
