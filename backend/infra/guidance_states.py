"""What a workspace's guidance history can answer about itself.

Two answers decide every history question here: a Git fault, which the next try may clear and which a
read reports as unavailable with a Retry that can work; and a state with no history of its own,
which is permanent until the workspace changes and which both the guidance save and the history read
call history off.
"""


class GitFault(RuntimeError):
    """A Git or OS failure; callers surface it and never read it as clean."""


class _LockBusy(GitFault):
    pass


class NoRepository(str):
    """The reason ``workspace_repository`` gives when the workspace has no repository of its own yet."""


class HistoryOff(str):
    """A workspace state with no guidance history of its own, permanent until the workspace changes:
    its Git directory resolves elsewhere, or it sits inside an enclosing repository. A guidance save
    treats it as history off and writes unrecorded; a read says history is off, never "try again"."""


def history_is_off(reason: str | None) -> bool:
    """Whether a ``workspace_repository`` reason is a state history is off for — the one
    classification both the guidance save and the history read use. A Git fault is not one: it may
    succeed on the next try, which is what a Retry is for."""
    return isinstance(reason, (NoRepository, HistoryOff))

