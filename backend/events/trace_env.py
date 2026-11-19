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
# No length or count limit: the loop guard never adds a rule already in the chain, so a real
# chain names each rule once, and rule ids have no length limit where they are made. The
# operating system's limit on a process environment bounds what a script can hand back.


def encode(trace: tuple[str, ...]) -> str:
    return json.dumps(list(trace))


def parse(raw: str) -> tuple[str, ...]:
    """The chain in ``raw``; ``ValueError`` names what is wrong with a malformed one."""
    shape = f"{TRACE_ENV} must be a JSON list of non-empty rule ids"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(f"{shape}; it is not valid JSON") from None
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(shape)
    return tuple(value)
