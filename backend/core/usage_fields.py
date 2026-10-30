"""The closed field list of an install's weekly usage record: the one definition.

A record says only that this install was active in an ISO week, on which release, and on
which OS family and chip family. Nothing in it identifies the install or a person. A new
field is added here by a reviewed change, never by an emitter. This module owns every value
rule (the patterns and enums below); the outcome-event allowlist builds its keys and rules
from them, and the recorder builds each value from a source that already satisfies them.
"""

from __future__ import annotations

USAGE_EVENT = "corsia.usage.install_week"
WEEK_FORMAT = "<ISO year>-W<ISO week>"
# Full-match patterns. ASCII digits only: a bare ``\d`` also matches other scripts' digits.
WEEK_PATTERN = r"[0-9]{4}-W(0[1-9]|[1-4][0-9]|5[0-3])"
DEV_RELEASE = "dev"
RELEASE_PATTERN = rf"[0-9]{{1,4}}\.[0-9]{{1,4}}\.[0-9]{{1,4}}|{DEV_RELEASE}"
OS_FAMILIES = ("darwin", "windows", "linux")
CHIPS = ("arm64", "amd64")
# Each field's value rule, by field: the one place the field list lives. The OS and chip are left
# out, not guessed, when this build is on neither list.
PATTERNS = {"corsia.usage.week": WEEK_PATTERN, "service.version": RELEASE_PATTERN}
ENUMS = {"os.type": OS_FAMILIES, "host.arch": CHIPS}
FIELDS = (*PATTERNS, *ENUMS)
