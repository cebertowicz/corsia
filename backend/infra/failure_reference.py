"""Public, non-secret references that correlate user-visible failures with logs."""

import re
import secrets
from typing import Literal, get_args

Prefix = Literal["cli", "local", "signin"]
# The one accept rule for a reference CORSIA minted; readers validate against it, never a copy.
PATTERN = rf"^(?:{'|'.join(get_args(Prefix))})-[0-9a-f]{{16}}$"
REFERENCE = re.compile(PATTERN)


def new_reference(prefix: Prefix = "cli") -> str:
    """Mint an opaque reference, labelled for the operation that failed."""
    return f"{prefix}-{secrets.token_hex(8)}"
