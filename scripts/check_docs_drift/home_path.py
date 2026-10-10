"""The home-directory path pattern shared by the scrub and private-scan checks.

Kept as a plain literal and explicitly exempted via `hardcoded-path-or-id`'s
own `allow_files` setting in `tools/antipatterns/default-config.toml`, rather
than routed through a data-file indirection to dodge the AST scan: this file
legitimately owns a canonical copy of that rule's own pattern, needed at
runtime by the docs-drift checks, and the exemption is recorded where an
owner reviewing the anti-pattern config can see and revoke it.

The one capture group isolates the segment after ``/Users/`` so callers can
classify it (e.g. an ellipsis placeholder) without re-deriving the prefix
length as a second, separately-maintained literal.
"""

from __future__ import annotations

import re

HOME_PATH_RE = re.compile(r"/Users/([^/\s`)\"']+)")
