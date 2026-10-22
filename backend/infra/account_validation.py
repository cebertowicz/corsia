"""Account input validation and operator-safe errors, independent of storage."""

from __future__ import annotations

import re

from infra.passwords import MIN_PASSWORD_LENGTH

#: Lowercase, so a username cannot differ from a data-owner id by case alone.
USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")


class AccountError(ValueError):
    """A request about an account is invalid — safe to show the operator."""


class FirstAccountExists(AccountError):
    """A first-run account request lost to an account already in the store."""


def normalize_username(raw: str) -> str:
    """Lowercase and validate ``raw``, or raise :class:`AccountError`."""
    name = (raw or "").strip().lower()
    if not USERNAME_PATTERN.match(name):
        raise AccountError(
            "Use up to 32 lowercase letters, numbers, dashes or underscores for the username."
        )
    return name


def require_password(password: str) -> None:
    """Reject a password too short to be worth hashing."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccountError(
            f"Use a password of at least {MIN_PASSWORD_LENGTH} characters."
        )
