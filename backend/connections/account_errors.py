"""Fixed, safe-to-show refusals from connected email or calendar accounts."""


class AccountError(Exception):
    """A fixed, safe-to-show refusal from a connected email or calendar account."""

    #: Only the person can clear it (sign in again, a password, a host setting); trying again can't.
    person_fixes = False


class AccountUnavailable(AccountError):
    """A refusal that clears on its own: trying again later can work, and reconnecting doesn't help."""


class AccountNeedsPerson(AccountError):
    """A refusal only the person can clear: sign in again, set a password, or change a host setting."""

    person_fixes = True
