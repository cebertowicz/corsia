"""Scrub-rule normalization for starter-kit vs workspace-source diffing.

The starter kit is a manually scrubbed public copy: the maintainer's
personal name and home-directory paths are replaced with generic phrasing
before a file is copied in. Normalizing both sides to the same placeholders
isolates real content drift from scrubbing; many files still differ after
normalization (rewritten prose, redacted incident detail, updated status
headers) -- that residue is genuine drift to report, not a bug here.

The person-name pattern is built once per run from the maintainer's actual
git identity (see `build_person_pattern`), never hardcoded here: this
module's own source must never carry the real name it exists to scrub. The
same combined pattern -- generic kit phrasing ("the owner", "the
maintainer(s)", "alice") plus the real name tokens -- is applied identically
to both the kit and the workspace text, so a generic phrase present on
*both* sides (e.g. a workspace doc that itself says "the owner") normalizes
on both sides instead of only one; an asymmetric pair of regexes here was
the earlier bug that produced false-positive drift for exactly that case.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .home_path import HOME_PATH_RE

_PERSON_TOKEN = "<PERSON>"
_HOME_TOKEN = "<HOME>"

# No word boundary: the same substitution must fire inside identifiers too
# (`ask_steve` -> `ask_<PERSON>`, matching the kit's own `ask_alice`).
_GENERIC_PERSON_PATTERN = r"the (?:project )?owner(?:'s)?|the maintainers?(?:'s)?|alice"


def build_person_pattern(real_name_tokens: Sequence[str]) -> re.Pattern[str]:
    """Combine the generic kit phrasing with the maintainer's real name tokens.

    `real_name_tokens` comes from the caller (e.g. `git config user.name`,
    split into words) -- this function never sources a real name itself.
    """
    parts = [re.escape(t) for t in real_name_tokens if t.strip()]
    pattern = _GENERIC_PERSON_PATTERN
    if parts:
        pattern = f"{pattern}|{'|'.join(parts)}"
    return re.compile(pattern, re.IGNORECASE)


def normalize_text(text: str, person_re: re.Pattern[str]) -> str:
    """Normalize either side's text with the same person pattern and home-path rule."""
    text = person_re.sub(_PERSON_TOKEN, text)
    return HOME_PATH_RE.sub(_HOME_TOKEN, text)
