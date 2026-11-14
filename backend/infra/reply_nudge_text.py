"""The words of the Focus nudge (``reply_not_visible_wake``): the unanswered-message form, the form for
a person who was sent an update but not every comment answer, and the first-run form."""

from __future__ import annotations

NO_REPLY = "NO_REPLY"


def pending_sentence(pending: list[tuple[str, str]] | None) -> str:
    """Comment answers the Turn wrote that the person has not been sent, by comment and first line."""
    if not pending:
        return ""
    listed = "; ".join(f'[{cid}] "{line}"' for cid, line in pending)
    return (
        f" Comment answers you wrote that they did not see: {listed}. NO_REPLY leaves them unsent."
        " To send them, use post_update with each answer in full, starting with its id in brackets."
    )


def nudge_text(
    hhmm: str, hidden: str | None, pending: list[tuple[str, str]] | None = None, *, replied: bool = False,
) -> str:
    """The approved #1904 wording; the quote sentence only when there are hidden words, and the
    pending-answers sentence only when the Turn holds tagged answers the person was not sent.
    ``replied``: they were sent an update, so only the unsent answers are asked about."""
    if replied:
        return (
            f"They have your update after their {hhmm} message, but not every comment answer you wrote."
            f"{pending_sentence(pending)} Answer with exactly {NO_REPLY} and nothing else to leave them unsent."
        )
    seen = (f' Your last words, which they did not see: "{hidden}"' if hidden else "") + pending_sentence(pending)
    return (
        f"No reply is visible to the user after their {hhmm} message.{seen} If you intended to "
        "send a visible response, please use post_update now. If no reply to their last "
        "message was necessary or expected, DO NOT SEND AN UPDATE. "
        f"Instead, answer with exactly {NO_REPLY} and nothing else."
    )


def first_run_nudge_text(hidden: str | None) -> str:
    seen = f' Your last words, which they did not see: "{hidden}"' if hidden else ""
    return (
        "No reply is visible to the user after setup started." + seen +
        " If you intended to send a visible response, please use post_update now. "
        "If no reply was necessary or expected, DO NOT SEND AN UPDATE. "
        f"Instead, answer with exactly {NO_REPLY} and nothing else."
    )
