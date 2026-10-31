"""The workspace's user profile, seeded once at install.

``context/50-USER-PROFILE.md`` reaches every agent as workspace context, so what
the person wants to be called and their timezone are known from the first turn.
Install writes it only when it is missing; an existing file is the person's and
is never changed there, except that an existing file naming no email gets that
single line appended. While setup is unfinished, ``stage_profile_update`` also
rewrites the full name, preferred name and email lines the account screen
collected, and touches no other line.

A write is staged beside its target and published by one rename, so the caller
can put the publish at the end of its own transaction and undo it if that
transaction does not commit. Every failure raises; nothing is reported through
a return value that also means "already exists".
"""

from __future__ import annotations

import hashlib
import logging
import os
import pwd
import stat
import string
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

PROFILE_RELATIVE_PATH = Path("context") / "50-USER-PROFILE.md"
_LOCALTIME = Path("/etc/localtime")
_EMAIL_LABEL = "- **Email:**"
_NAME_LABEL = "- **Preferred name:**"
_FULL_NAME_LABEL = "- **Full name:**"
USE_COMMUNITY_LABEL = "- **Use community improvements:**"
SHARE_COMMUNITY_LABEL = "- **Share improvements with the community:**"


def mac_full_name() -> str:
    """The full name on this Mac's login account, or empty when it has none."""
    try:
        record = pwd.getpwuid(os.getuid())
    except KeyError:
        return ""
    return one_line(record.pw_gecos.split(",", 1)[0])


def mac_timezone() -> str:
    """The Mac's IANA timezone (for example America/Los_Angeles), or empty."""
    try:
        target = _LOCALTIME.resolve(strict=True)
    except OSError:
        return ""
    _, marker, zone = str(target).partition("zoneinfo/")
    if not marker:
        return ""
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ""
    return zone


def one_line(value: str) -> str:
    """``value`` on one line with runs of whitespace collapsed; empty when it is only whitespace."""
    return " ".join(value.split())


def preferred_name(full_name: str) -> str:
    """Return the full name's printable, bounded first word for a profile greeting."""
    first = one_line(full_name).split(" ", 1)[0]
    return _printable_text(first)


def _printable_text(value: str) -> str:
    """Return one readable profile value without controls, bounded for context."""
    return one_line("".join(char for char in value if char.isprintable()))[:120]


def profile_text(full_name: str, timezone: str, email: str = "") -> str:
    """The profile body: the preferred name (the full name's first word), the full name, the email when
    known, and the timezone when the Mac reports one."""
    full_name = one_line(full_name)
    lines = ["# User profile", "", f"{_NAME_LABEL} {preferred_name(full_name)}", f"{_FULL_NAME_LABEL} {full_name}"]
    if one_line(email):
        lines.append(f"{_EMAIL_LABEL} {one_line(email)}")
    if timezone:
        lines.append(f"- **Timezone:** {timezone}. Give every time and timestamp in this zone, never UTC.")
    return "\n".join(lines) + "\n"


def _labeled_value(text: str, label: str) -> str:
    """The rest of the first line that starts with ``label``, or empty."""
    for line in text.splitlines():
        if line.startswith(label):
            return line.removeprefix(label)
    return ""


def _read_profile(workspace: Path) -> str:
    try:
        return (Path(workspace) / PROFILE_RELATIVE_PATH).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def _email_in(text: str) -> str:
    return _labeled_value(text, _EMAIL_LABEL).strip()


def profile_email(workspace: Path) -> str:
    """The email an existing profile already names, or empty."""
    return _email_in(_read_profile(workspace))


def _printable_value(workspace: Path, label: str) -> str:
    """The existing profile's value after ``label``, without control characters, or empty."""
    return _printable_text(_labeled_value(_read_profile(workspace), label))


def profile_name(workspace: Path) -> str:
    """The existing profile's preferred name, without control characters, or empty."""
    return _printable_value(workspace, _NAME_LABEL)


def profile_full_name(workspace: Path) -> str:
    """The existing profile's full name, without control characters, or empty."""
    return _printable_value(workspace, _FULL_NAME_LABEL)


def uses_community_improvements(workspace: Path) -> bool:
    """Whether the person explicitly answered yes to using community improvements."""
    words = _labeled_value(_read_profile(workspace), USE_COMMUNITY_LABEL).split(maxsplit=1)
    return bool(words) and words[0].rstrip(string.punctuation).casefold() == "yes"


class StagedProfile:
    """One profile write prepared in a temp file beside its target.

    Nothing the person can see changes until :meth:`publish`. ``previous`` is
    the text being extended, or ``None`` when the profile is being created.
    """

    def __init__(self, path: Path, temp: Path, previous: str | None, text: str) -> None:
        self._path = path
        self._temp = temp
        self._previous = previous
        self._published = False
        #: The digest of the file this write leaves, and of the one it creates (None when it extends the person's own).
        self.sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.created_sha256 = self.sha256 if previous is None else None

    def publish(self) -> None:
        """Move the staged file into place with one rename; on failure nothing has changed."""
        if self._previous is None and self._path.exists():
            raise FileExistsError("the user profile appeared while setup was running")
        os.replace(self._temp, self._path)
        self._published = True

    def undo(self) -> None:
        """Leave the profile as it was before staging; never raises, and logs what it could not undo."""
        try:
            if not self._published:
                self._temp.unlink(missing_ok=True)
            elif self._previous is None:
                self._path.unlink(missing_ok=True)
            else:
                self._path.write_text(self._previous, encoding="utf-8")
        except OSError:
            logger.exception("Could not restore the user profile")


def _stage_text(path: Path, text: str, previous: str | None, mode: int | None) -> StagedProfile:
    """Write ``text`` beside ``path`` (keeping ``mode`` when the file exists) and return the staged publish."""
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(temp, mode)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return StagedProfile(path, temp, previous, text)


def stage_user_profile(workspace: Path, full_name: str, timezone: str, email: str = "") -> StagedProfile | None:
    """Prepare the profile write for a new install, or ``None`` when there is nothing to write.

    A missing profile is created from the full name, timezone and email. An existing
    one is never rewritten; it only gains the email it names none of. Raises
    ``ValueError`` when the name is empty and ``OSError`` when the folder cannot
    be written.
    """
    if not one_line(full_name):
        raise ValueError("a name is required")
    path = Path(workspace) / PROFILE_RELATIVE_PATH
    email = one_line(email)
    previous: str | None = None
    mode: int | None = None
    if path.exists():
        previous = path.read_text(encoding="utf-8")
        if not email or _email_in(previous):
            return None
        separator = "" if previous.endswith("\n") or not previous else "\n"
        text = f"{previous}{separator}{_EMAIL_LABEL} {email}\n"
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = profile_text(full_name, timezone, email)
    return _stage_text(path, text, previous, mode)


def _with_line(lines: list[str], label: str, value: str, *, after: str | None = None) -> list[str]:
    """``lines`` with the one ``label`` line set to ``value`` (removed when empty), kept where it was or put after ``after``."""
    kept = [line for line in lines if not line.startswith(label)]
    if not value:
        return kept
    new = f"{label} {value}"
    for index, line in enumerate(lines):
        if line.startswith(label):
            return [*lines[:index], new, *[rest for rest in lines[index + 1:] if not rest.startswith(label)]]
    place = max((i for i, line in enumerate(kept) if after and line.startswith(after)), default=len(kept) - 1)
    return [*kept[:place + 1], new, *kept[place + 1:]]


def stage_profile_update(workspace: Path, *, full_name: str | None = None, email: str | None = None) -> StagedProfile | None:
    """Prepare a change to the name and email the person gave during setup, leaving every other line as it is.

    ``full_name`` also resets the preferred name to its first word; ``email`` empty removes the line. ``None`` leaves
    that field alone. A missing profile is created from a name and refused (``FileNotFoundError``) for an email alone.
    Returns ``None`` when the file would not change. Raises ``ValueError`` for a blank name and ``OSError`` when the
    folder cannot be written.
    """
    if full_name is not None and not one_line(full_name):
        raise ValueError("a name is required")
    path = Path(workspace) / PROFILE_RELATIVE_PATH
    if not path.exists():
        if full_name is None:
            raise FileNotFoundError(f"no user profile at {path} to put the email in")
        return stage_user_profile(workspace, full_name, mac_timezone(), email or "")
    previous = path.read_text(encoding="utf-8")
    lines = previous.splitlines()
    if full_name is not None:
        name = one_line(full_name)
        lines = _with_line(lines, _FULL_NAME_LABEL, name, after="# User profile")
        lines = _with_line(lines, _NAME_LABEL, name.split(" ", 1)[0], after="# User profile")
    if email is not None:
        lines = _with_line(lines, _EMAIL_LABEL, one_line(email), after=_FULL_NAME_LABEL)
    text = "\n".join(lines) + "\n"
    if text == previous:
        return None
    return _stage_text(path, text, previous, stat.S_IMODE(path.stat().st_mode))


def stage_community_answer(workspace: Path, *, use: bool, share: bool) -> StagedProfile | None:
    """Prepare the two community lines the person's answer sets, leaving every other line as it is.

    Each line is replaced where it is, or added at the end. A missing profile is created holding only
    its heading and these lines; publishing refuses if one appeared meanwhile. Returns ``None`` when
    the file would not change. Raises ``OSError`` when the folder cannot be written.
    """
    path = Path(workspace) / PROFILE_RELATIVE_PATH
    previous: str | None = None
    mode: int | None = None
    lines = ["# User profile", ""]
    if path.exists():
        previous = path.read_text(encoding="utf-8")
        lines = previous.splitlines()
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    lines = _with_line(lines, USE_COMMUNITY_LABEL, "Yes" if use else "No")
    lines = _with_line(lines, SHARE_COMMUNITY_LABEL, "Yes" if share else "No", after=USE_COMMUNITY_LABEL)
    text = "\n".join(lines) + "\n"
    if text == previous:
        return None
    return _stage_text(path, text, previous, mode)
