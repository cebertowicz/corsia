"""Fixed, safe-to-show refusals from connected email or calendar accounts."""


class AccountError(Exception):
    """A fixed, safe-to-show refusal from a connected email or calendar account.

    ``person_fixes`` is the one answer to "can only the person clear this?" (sign in again, a password, a host
    setting): the raiser sets it, and every reader trusts it. Unset, trying again later can work.
    """

    def __init__(self, *args: object, person_fixes: bool = False) -> None:
        super().__init__(*args)
        self.person_fixes = person_fixes
