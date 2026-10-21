"""Agent-facing text for a project that turned cloud models off: one source, one owner.

Governance & Scopes owns this wording. Only a person can turn cloud models back on, so
no text here tells an agent to do it.
"""

from __future__ import annotations

HOLD_PHRASE = "this project uses local models only"
#: The ``thread_local_only`` cause: the chat's own setting, lifted only by moving it.
THREAD_HOLD_PHRASE = "this chat is set to local models only"
#: The same cause as the parent reads it: "this chat" would read as the parent's own.
WORKER_HOLD_PHRASE = "the worker is set to local models only"
#: Each cause in ``llm.locality.LOCALITY_REFUSALS``, in the words its own policy text uses.
LOCALITY_PHRASES = {"project_cloud_off": HOLD_PHRASE, "thread_local_only": THREAD_HOLD_PHRASE}

_USE_READY_LOCAL = "Use a ready local model"
#: With no local model ready, the only recovery is a thread already on one: never the refused model.
NO_LOCAL_READY_TEXT = (
    "No local model is ready now. Do the work in a thread already on a local model,"
    " or check model_catalog later."
)

def _hold_action(subject: str) -> str:
    """What to do about a held ``subject``: its current model is the cause, so staying on it is no recovery."""
    return (
        f"Move the {subject} to a ready local model from model_catalog (delegate_set_model). "
        "The held message runs once it is on one. "
        "If none is ready, do the work in a thread already on a local model."
    )


#: The held thread's own recovery.
HOLD_ACTION = _hold_action("thread")
#: The same recovery as the parent reads it, naming which thread to move.
WORKER_HOLD_ACTION = _hold_action("worker")


def thread_local_only_text(selector: str, ready_local: list[str], *, spawn: bool = False) -> str:
    """Return the agent-facing recovery text for a ``thread_local_only`` refusal."""
    text = f"local_only_requires_local_model: {_held_clause(selector, 'thread_local_only')}. "
    text += f"{_USE_READY_LOCAL}: {', '.join(ready_local)}." if ready_local else NO_LOCAL_READY_TEXT
    return f"{text} Local workers run one at a time." if spawn else text


def _held_clause(selector: str, cause: str, thread_phrase: str = THREAD_HOLD_PHRASE) -> str:
    if cause == "thread_local_only":
        return f"{selector} can't run here: {thread_phrase}"
    return f"{HOLD_PHRASE}, so {selector} can't run here"


def held_error(selector: str, cause: str = "project_cloud_off") -> str:
    """The Turn-end text for an input held because locality policy forbids ``selector``."""
    return f"Held: {_held_clause(selector, cause)}. {HOLD_ACTION}"


def parent_wake_template(cause: str) -> str:
    """The parent's wake line for a child held by locality policy, with ``{title}`` and ``{model}``.

    Only the person can lift a project's policy, so the recovery named is moving the worker
    itself; the held message then runs without being sent again.
    """
    return (
        "[CORSIA plan] The child delegated for {title} is held: "
        + _held_clause("{model}", cause, WORKER_HOLD_PHRASE)
        + f". Its plan and history are intact. {WORKER_HOLD_ACTION}"
    )
