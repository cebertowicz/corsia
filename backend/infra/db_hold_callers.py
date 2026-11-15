"""Call-site diagnostics for slow SQLite write holds."""

import contextlib
import os
import sys

_PLUMBING_FILES = frozenset({
    os.path.normcase(os.path.join(os.path.dirname(__file__), "database.py")),
    os.path.normcase(contextlib.__file__),
})


def hold_callers() -> str:
    """The nearest four callers holding a slow write, skipping transaction plumbing."""
    frames: list[str] = []
    frame = sys._getframe(1)
    while frame is not None and len(frames) < 4:
        code = frame.f_code
        if os.path.normcase(code.co_filename) not in _PLUMBING_FILES:
            frames.append(f"{os.path.basename(code.co_filename)}:{frame.f_lineno}:{code.co_name}")
        frame = frame.f_back
    return " < ".join(frames) or "unknown"
