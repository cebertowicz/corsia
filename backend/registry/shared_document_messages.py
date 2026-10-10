"""Agent-facing refusal text for writes under a scope's ``shared/`` tree."""

from __future__ import annotations

from pathlib import Path

from registry.shared_principals import owner_label, scope_relative


def missing_category_message(
    category: str,
    approved: tuple[str, ...],
    conductor_id: str | None,
) -> str:
    choices = ", ".join(approved) if approved else "none"
    if conductor_id:
        next_action = (
            f"Ask conductor thread {conductor_id} to reuse an existing category "
            "or create the category and its INDEX.md."
        )
    else:
        next_action = "Ask the user to create or choose an approved shared category."
    return (
        f"Shared category {category!r} is not approved. "
        f"Approved categories: {choices}. {next_action}"
    )


def structure_owned_message(surface: str, conductor_id: str | None) -> str:
    who = f"conductor thread {conductor_id}" if conductor_id else "the scope conductor"
    return (
        f"{surface} is scope structure owned by {who} — it applies to every "
        "agent and thread in this scope. A thread may write anywhere inside "
        "its own .threads/<id>/ directory, or into an approved "
        f"shared/<category>/. Ask {who} to change scope context, skills, or structure."
    )


def _suggest_instruction(path: Path) -> str:
    return (
        "To change it, call `doc_comment_create` with "
        f"`doc_path` set to `{path}`, `quote` (the exact current text) and `replacement`; "
        "the owner applies it. To raise a point without a text change, comment instead."
    )


def owner_refusal(path: Path, scope_root: Path, owner: str, *, db) -> str:
    """The refusal for a non-owner write to an owned document."""
    label = owner_label(owner, db=db)
    return (
        f"{scope_relative(path, scope_root)} is owned by {label}. Only its owner, the conductor, or the user "
        f"edits it directly. {_suggest_instruction(path)}"
    )


def bypass_notice(path_rel: str, owner_title: str) -> str:
    """The notice for a writer whose refused write landed anyway, outside the file tools."""
    return (
        f"You changed `{path_rel}`, owned by {owner_title}, outside the file tools; "
        f"{owner_title} has been told. Suggest changes with `doc_comment_create` instead."
    )


def accept_refusal(owner_title: str) -> str:
    """The refusal when a non-owner agent tries to accept or reject a suggestion."""
    return (
        f"Only the document's owner ({owner_title}), the conductor, or the user can accept or "
        "reject suggestions. To propose a change, suggest it with `doc_comment_create` "
        "(`doc_path`, `quote`, `replacement`); to raise a point, reply to the suggestion."
    )
