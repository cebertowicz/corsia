"""Public, non-secret references that correlate user-visible failures with logs."""

import re
import secrets
from typing import Literal, get_args

Prefix = Literal["cli", "local", "signin"]
# Crockford base32: no I, L, O or U, so a reference survives being read aloud.
_SPOKEN_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# The one accept rule for a reference CORSIA minted; readers validate against it, never a copy.
PATTERN = rf"^(?:(?:{'|'.join(get_args(Prefix))})-[0-9a-f]{{16}}|[{_SPOKEN_ALPHABET}]{{6}})$"
REFERENCE = re.compile(PATTERN)


def new_reference(prefix: Prefix = "cli") -> str:
    """Mint an opaque reference, labelled for the operation that failed."""
    return f"{prefix}-{secrets.token_hex(8)}"


def new_error_reference() -> str:
    """Mint a six-character reference a person can read aloud, for API and turn errors."""
    return "".join(secrets.choice(_SPOKEN_ALPHABET) for _ in range(6))
