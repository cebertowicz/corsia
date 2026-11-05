"""Classify host paths for reviewed access."""

from pathlib import Path


def path_review_classification(
    *paths: Path,
    for_write: bool,
) -> tuple[bool, str | None]:
    """Classify the lexical and resolved forms of one host action path.

    Returns ``(needs_review, hard_floor_denial)``. A hard-floor denial wins and
    is never lease-eligible; otherwise ``needs_review`` says whether any form
    is refused in protected mode and so needs a reviewed lease.
    """
    from tools.host_path_policy import current_protected_host_path_policy, ordinary_host_path_denial

    forms = tuple(dict.fromkeys(paths))
    policy = current_protected_host_path_policy()
    for form in forms:
        if message := policy.hard_floor_denial(form, for_write=for_write):
            return True, message
    return any(
        ordinary_host_path_denial(form, for_write=for_write) for form in forms
    ), None
