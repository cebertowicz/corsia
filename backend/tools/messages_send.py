"""Hand one approved text to the Messages app with ``osascript`` and confirm it appeared in ``chat.db``.

The person's text and the recipient travel as ``argv``, never inside the script source. A run that
fails with one of the codes below proves nothing was sent; every other outcome is unknown.
"""

from __future__ import annotations

import asyncio
import re

import tools.messages_texts as texts
from tools import messages_db

OSASCRIPT = "/usr/bin/osascript"
CONFIRM_SECONDS = 5.0
POLL_SECONDS = 0.5
RUN_SECONDS = 30.0
#: AppleScript error numbers that mean Messages never got the text: Automation denied, app can't start, no such chat.
NEVER_SENT = {"-1743": texts.NO_AUTOMATION, "-600": texts.NO_MESSAGES_APP, "-1728": texts.NO_SUCH_CHAT}
_TO_CHAT = (
    "on run argv",
    'tell application "Messages" to send (item 2 of argv) to chat id (item 1 of argv)',
    "end run",
)
_TO_HANDLE = (
    "on run argv",
    'tell application "Messages" to send (item 2 of argv) to participant (item 1 of argv) '
    "of (1st service whose service type = iMessage)",
    "end run",
)


class SendFailed(Exception):
    """osascript exited non-zero; ``never_sent`` says whether that proves nothing went out."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        code = re.search(r"\((-\d+)\)", detail)
        self.fixed = NEVER_SENT.get(code.group(1)) if code else None

    @property
    def never_sent(self) -> bool:
        return self.fixed is not None


async def _osascript(lines: tuple[str, ...], *args: str) -> None:
    # getopt stops at the first argument (a chat guid or handle, never ``-…``), so the text after it stays an argument.
    command = [OSASCRIPT, *(part for line in lines for part in ("-e", line)), *args]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), RUN_SECONDS)
    except asyncio.TimeoutError:
        process.kill()
        raise
    if process.returncode != 0:
        raise SendFailed(stderr.decode("utf-8", "replace"))


def _confirmed(chat_guid: str | None, handle: str | None, body: str, since: int) -> str | None:
    try:
        db = messages_db.open_db()
    except messages_db.MessagesError:
        return None
    try:
        return messages_db.sent_row(db, chat_guid, handle, body, since)
    finally:
        db.close()


async def send(chat_guid: str | None, handle: str | None, body: str) -> str | None:
    """Ask Messages to send; return the new row's guid once ``chat.db`` shows it, else None (unconfirmed)."""
    since = messages_db.now_ns() - 2_000_000_000
    if chat_guid:
        await _osascript(_TO_CHAT, chat_guid, body)
    else:
        await _osascript(_TO_HANDLE, str(handle), body)
    waited = 0.0
    while True:
        found = await asyncio.to_thread(_confirmed, chat_guid, handle, body, since)
        if found is not None or waited >= CONFIRM_SECONDS:
            return found
        await asyncio.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
