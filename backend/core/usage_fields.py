"""The closed field list of an install's weekly usage summary: the one definition.

A summary says only that this install was active in an ISO week, on which release, OS
family and chip family. Nothing identifies the install or a person. A new field family
is added here by a reviewed change, never by an emitter; every reader and writer
(local recording, the summary builder, intake) reads this one list.
"""

from __future__ import annotations

USAGE_EVENT = "corsia.usage.install_week"
WEEK_FORMAT = "<ISO year>-W<ISO week>"
OS_FAMILIES = ("macos", "windows", "linux")
CHIPS = ("arm64", "x86_64")
DEV_RELEASE = "dev"
# Required fields; the OS and chip are left out, not guessed, when this build is on neither list.
REQUIRED_FIELDS = ("week", "release")
OPTIONAL_FIELDS = ("os", "chip")
FIELDS = (*REQUIRED_FIELDS, *OPTIONAL_FIELDS)
