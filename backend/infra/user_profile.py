"""The workspace's user profile, seeded once at install.

``context/50-USER-PROFILE.md`` reaches every agent as workspace context, so what
the person wants to be called and their timezone are known from the first turn.
Install writes it only when it is missing; an existing file is the person's and
is never changed. The one exception is the email: an existing file that names
none gets that single line appended, and one that names an email is only read.
"""

from __future__ import annotations

import logging
import os
import pwd
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
    return _one_line(record.pw_gecos.split(",", 1)[0])


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


def _one_line(value: str) -> str:
    return " ".join(value.split())


def profile_text(name: str, timezone: str, email: str = "") -> str:
    """The profile body: the name, the email when known, and the timezone when the Mac reports one."""
    lines = ["# User profile", "", f"- **Preferred name:** {_one_line(name)}"]
    if _one_line(email):
        lines.append(f"{_EMAIL_LABEL} {_one_line(email)}")
    if timezone:
        lines.append(f"- **Timezone:** {timezone}. Give every time and timestamp in this zone, never UTC.")
    return "\n".join(lines) + "\n"


def profile_email(workspace: Path) -> str:
    """The email an existing profile already names, or empty."""
    try:
        text = (Path(workspace) / PROFILE_RELATIVE_PATH).read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        if line.startswith(_EMAIL_LABEL):
            return line[len(_EMAIL_LABEL):].strip()
    return ""


def add_profile_email(workspace: Path, email: str) -> bool:
    """Append the email to an existing profile that names none; True when this call wrote it."""
    email = _one_line(email)
    path = Path(workspace) / PROFILE_RELATIVE_PATH
    if not email or not path.is_file() or profile_email(workspace):
        return False
    try:
        text = path.read_text(encoding="utf-8")
        separator = "" if text.endswith("\n") or not text else "\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{separator}{_EMAIL_LABEL} {email}\n")
    except OSError:
        logger.warning("Could not add the email to the user profile", exc_info=True)
        return False
    return True


def write_user_profile(workspace: Path, name: str, timezone: str, email: str = "") -> bool:
    """Create the profile if it is missing; True when this call created it."""
    if not _one_line(name):
        return False
    path = Path(workspace) / PROFILE_RELATIVE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(profile_text(name, timezone, email))
    except FileExistsError:
        return False
    except OSError:
        logger.warning("Could not write the user profile", exc_info=True)
        return False
    return True
