"""The person's answer to the community decision, written to their profile.

Answering ``setup_strings.COMMUNITY`` is the consent that turns the library on or
keeps it off, so CORSIA records it in ``context/50-USER-PROFILE.md`` itself
rather than relying on the agent to. Any other decision, owner, or an answer
that picks no option writes nothing. A failed write is logged and never undoes
the answer.
"""

from __future__ import annotations

import logging
from pathlib import Path

from infra import user_profile

log = logging.getLogger(__name__)


def community_answer(decision: dict, chosen: dict) -> tuple[bool, bool] | None:
    """The (use, share) answer the chosen option stands for, or None when this is another decision or no listed option.

    Options are matched by label text against ``setup_strings.COMMUNITY``, so reordering them cannot remap consent.
    """
    from onboarding import setup_strings

    if decision.get("title") != setup_strings.COMMUNITY["title"]:
        return None
    labels = {option["id"]: option["label"] for option in decision.get("options", [])}
    chosen_label = labels.get(chosen.get("option_id"))
    for label, _detail, answer in setup_strings.COMMUNITY["options"]:
        if label == chosen_label:
            return answer
    return None


def write_profile_lines(workspace: Path, answer: tuple[bool, bool]) -> None:
    """Write the two lines; never raises."""
    try:
        staged = user_profile.stage_community_answer(workspace, use=answer[0], share=answer[1])
        if staged is None:
            return
        try:
            staged.publish()
        except BaseException:
            staged.undo()
            raise
    except Exception:  # noqa: BLE001 - the answer is already committed
        log.warning("community answer not written to the user profile", exc_info=True)
