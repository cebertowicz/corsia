"""The workspace's user profile, seeded once at install.

``context/50-USER-PROFILE.md`` reaches every agent as workspace context, so what
the person wants to be called and their timezone are known from the first turn.
Install writes it only when it is missing; an existing file is the person's and
is never changed. The one exception is the email: an existing file that names
none gets that single line appended, and one that names an email is only read.

A write is staged beside its target and published by one rename, so the caller
can put the publish at the end of its own transaction and undo it if that
transaction does not commit. Every failure raises; nothing is reported through
a return value that also means "already exists".
"""

from __future__ import annotations

import logging
import os
import pwd
import stat
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

PROFILE_RELATIVE_PATH = Path("context") / "50-USER-PROFILE.md"
_LOCALTIME = Path("/etc/localtime")
_EMAIL_LABEL = "- **Email:**"


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


def profile_text(name: str, timezone: str, email: str = "") -> str:
    """The profile body: the name, the email when known, and the timezone when the Mac reports one."""
    lines = ["# User profile", "", f"- **Preferred name:** {one_line(name)}"]
    if one_line(email):
        lines.append(f"{_EMAIL_LABEL} {one_line(email)}")
    if timezone:
        lines.append(f"- **Timezone:** {timezone}. Give every time and timestamp in this zone, never UTC.")
    return "\n".join(lines) + "\n"


def _email_in(text: str) -> str:
    for line in text.splitlines():
        if line.startswith(_EMAIL_LABEL):
            return line[len(_EMAIL_LABEL):].strip()
    return ""


def profile_email(workspace: Path) -> str:
    """The email an existing profile already names, or empty."""
    try:
        return _email_in((Path(workspace) / PROFILE_RELATIVE_PATH).read_text(encoding="utf-8"))
    except OSError:
        return ""


class StagedProfile:
    """One profile write prepared in a temp file beside its target.

    Nothing the person can see changes until :meth:`publish`. ``previous`` is
    the text being extended, or ``None`` when the profile is being created.
    """

    def __init__(self, path: Path, temp: Path, previous: str | None) -> None:
        self._path = path
        self._temp = temp
        self._previous = previous
        self._published = False

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


def stage_user_profile(workspace: Path, name: str, timezone: str, email: str = "") -> StagedProfile | None:
    """Prepare the profile write for a new install, or ``None`` when there is nothing to write.

    A missing profile is created from the name, timezone and email. An existing
    one is never rewritten; it only gains the email it names none of. Raises
    ``ValueError`` when the name is empty and ``OSError`` when the folder cannot
    be written.
    """
    if not one_line(name):
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
        text = profile_text(name, timezone, email)
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
    return StagedProfile(path, temp, previous)
