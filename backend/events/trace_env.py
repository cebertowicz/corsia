"""The rule chain a rule-started script run hands to ``corsia event emit``.

The script runner sets ``CORSIA_EVENT_TRACE`` (a JSON list of rule ids) only for
a run an event rule started, so an event the script emits through the CLI
carries the same chain as one it prints, and the loop guard
(``events.rule_runs``) sees it. A script that clears the variable can still
loop; the per-rule queue cap bounds that. Imports nothing, so the shell
environment can name it.
"""

from __future__ import annotations

import json

TRACE_ENV = "CORSIA_EVENT_TRACE"
MAX_TRACE_RULES = 100
MAX_RULE_ID_CHARS = 64
MAX_TRACE_CHARS = 8192


def encode(trace: tuple[str, ...]) -> str:
    return json.dumps(list(trace))


def parse(raw: str) -> tuple[str, ...]:
    """The chain in ``raw``; ``ValueError`` names what is wrong with a malformed one."""
    shape = (f"{TRACE_ENV} must be a JSON list of at most {MAX_TRACE_RULES} rule ids, "
             f"each 1-{MAX_RULE_ID_CHARS} characters")
    if len(raw) > MAX_TRACE_CHARS:
        raise ValueError(f"{shape}; it is longer than {MAX_TRACE_CHARS} characters")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"{shape}; it is not valid JSON") from None
    if (not isinstance(value, list) or len(value) > MAX_TRACE_RULES
            or not all(isinstance(item, str) and 0 < len(item) <= MAX_RULE_ID_CHARS for item in value)):
        raise ValueError(shape)
    return tuple(value)
