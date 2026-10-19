"""Agent-facing text for a project that turned cloud models off: one source, one owner.

Governance & Scopes owns this wording. Only a person can turn cloud models back on, so
no text here tells an agent to do it.
"""

from __future__ import annotations

HOLD_PHRASE = "this project uses local models only"

_USE_READY_LOCAL = "Use a ready local model"
_STAY_INLINE = "stay on your current model and do the work inline, or check model_catalog later."

NO_LOCAL_READY_TEXT = f"No local model is ready now. {_STAY_INLINE[0].upper()}{_STAY_INLINE[1:]}"

#: What to do about a held thread: its current model is the cause, so staying on it is no recovery.
HOLD_ACTION = (
    "Move the thread to a ready local model from model_catalog (delegate_set_model). "
    "The held message runs once it is on one. "
    "If none is ready, do the work in a thread already on a local model."
)


def project_cloud_off_text(selector: str, ready_local: list[str], *, spawn: bool = False) -> str:
    """Return the agent-facing recovery text for a ``project_cloud_off`` refusal."""
    text = f"project_cloud_off: This project uses local models only, so {selector} can't run here. "
    text += f"{_USE_READY_LOCAL}: {', '.join(ready_local)}." if ready_local else NO_LOCAL_READY_TEXT
    return f"{text} Local workers run one at a time." if spawn else text


def held_error(selector: str) -> str:
    """The Turn-end text for an input held because its project forbids ``selector``."""
    return f"Held: {HOLD_PHRASE}, so {selector} can't run here. {HOLD_ACTION}"
