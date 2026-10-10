"""Scaffold layout content: what a workspace or project scaffold contains.

The layout builders here produce a ``_Layout`` value only; installing one onto
disk, with its atomic-rename and failure-compensation guarantees, is the
mechanism in ``core.workspace_scaffolding``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _Layout:
    directories: tuple[str, ...]
    files: tuple[tuple[str, str], ...]


def _workspace_layout() -> _Layout:
    return _Layout(
        directories=("context", "skills", "shared", "projects", ".threads"),
        files=(
            (
                "INDEX.md",
                "# Workspace\n\n"
                "- `context/` — workspace context documents.\n"
                "- `skills/` — workspace skills.\n"
                "- `shared/` — categorized durable shared material.\n"
                "- `projects/` — project workspaces.\n"
                "- `.threads/` — thread-local context, skills, and scratch.\n",
            ),
            ("AGENTS.md", _offline_connector("Codex", project=False)),
            ("CLAUDE.md", _offline_connector("Claude Code", project=False)),
            (
                ".gitignore",
                "# Thread-local scratch is ephemeral.\n"
                ".threads/*/working/\n"
                "projects/*/.threads/*/working/\n"
                "\n# Project source checkouts are separate repositories.\n"
                "projects/*/source/\n"
                "\n# Finder metadata is not workspace truth.\n"
                ".DS_Store\n",
            ),
            (
                "shared/INDEX.md",
                "# Shared Workspace Material\n\n"
                "Durable shared files belong in an approved category directory.\n",
            ),
        ),
    )


def _project_layout(name: str, description: str) -> _Layout:
    description_text = description.strip() or "_(Describe this project.)_"
    return _Layout(
        directories=("context", "skills", "shared", ".threads", "source"),
        files=(
            (
                "INDEX.md",
                f"# {name}\n\n"
                "- `context/` — project context documents.\n"
                "- `skills/` — project skills.\n"
                "- `shared/` — categorized durable shared material.\n"
                "- `.threads/` — thread-local context, skills, and scratch.\n"
                "- `source/` — project repositories.\n",
            ),
            ("AGENTS.md", _offline_connector("Codex", project=True)),
            ("CLAUDE.md", _offline_connector("Claude Code", project=True)),
            ("context/PROJECT.md", _project_charter(name, description_text)),
            (
                "context/COORDINATION-MODEL.md",
                _project_coordination_model(name),
            ),
            (
                "shared/INDEX.md",
                "# Shared Project Material\n\n"
                "Durable shared files belong in an approved category directory.\n",
            ),
        ),
    )


def _offline_connector(harness: str, *, project: bool) -> str:
    if project:
        return (
            f"# Offline {harness} Connector\n\n"
            "Generated pointer shell for direct offline use. CORSIA-managed sessions "
            "receive context and tools through CORSIA.\n\n"
            "Before acting, enumerate every top-level Markdown file in `context/` and "
            "read them in canonical authority order:\n\n"
            "1. `context/PROJECT.md`\n"
            "2. vision and intent files, in filename order\n"
            "3. safety and invariant files, in filename order\n"
            "4. all other project context files, in filename order\n"
            "5. `context/COORDINATION-MODEL.md`\n\n"
            "`context/PROJECT.md` and `context/COORDINATION-MODEL.md` are required. "
            "If either is missing, or any discovered source cannot be read, stop and "
            "report it instead of proceeding with partial project policy.\n"
        )
    return (
        f"# Offline {harness} Connector\n\n"
        "Generated pointer shell for direct offline use. CORSIA-managed sessions "
        "receive context and tools through CORSIA.\n\n"
        "Before acting, enumerate and read every top-level Markdown file in "
        "`context/` in filename order.\n\n"
        "If a required source cannot be read, stop and report it instead of "
        "proceeding with partial workspace policy.\n"
    )


def _project_charter(name: str, description: str) -> str:
    """Return the compact project-wide charter scaffold."""
    return (
        f"# {name}\n\n"
        "## Purpose\n"
        f"{description}\n\n"
        "## Goal hierarchy\n\n"
        "### Long-horizon objectives\n"
        "- _(Directional outcome and the trade-off when goals compete.)_\n\n"
        "### Current strategic objectives\n"
        "- _(Current objective, success evidence, and review horizon—not live task state.)_\n\n"
        "## User position and mandate\n"
        "- **Relationship:** _(Owner, operator, contributor, advisor, approver, or another actual position.)_\n"
        "- **Accountable for:** _(Outcomes and obligations.)_\n"
        "- **May decide:** _(Substantive decision rights.)_\n"
        "- **May execute:** _(Actions the user may perform or direct.)_\n"
        "- **May commit or represent:** _(What the user may promise, publish, approve, or communicate for others.)_\n"
        "- **Requires another authority for:** _(Approval chain and boundaries.)_\n\n"
        "## Constraints and non-goals\n"
        "- _(Hard constraint or outcome deliberately not pursued.)_\n\n"
        "## Durable risks, assumptions, and blind spots\n"
        "- _(Durable item, observable signal, and expected response. Keep live risks in work state.)_\n\n"
        "## Stable project topology\n"
        "- _(Resource or system, location, and purpose.)_\n\n"
        "## Canonical sources\n"
        "- _(Question, authoritative source, and freshness rule.)_\n"
    )


def _project_coordination_model(name: str) -> str:
    """Return the compact project-wide coordination scaffold."""
    return (
        f"# {name} — coordination model\n\n"
        "This is the shared project operating and context-delivery contract. "
        "It is not live status, an agent-specific manual, or the conductor's complete project graph.\n\n"
        "## Leverage objective\n"
        "_(What coordination burden should be removed from the user, and what better outcome should result?)_\n\n"
        "## Operating model\n"
        "- **User:** _(Project-wide substantive authority and responsibility.)_\n"
        "- **Conductor:** Designs and maintains coordination within the user's established mandate.\n"
        "- **Persistent agents:** Own durable responsibilities and receive additive responsibility-specific context.\n"
        "- **Temporary delegates:** Receive bounded task context and return evidence to their parent.\n\n"
        "## Context layers\n"
        "- **Project bootloader:** Stable truth every project thread needs.\n"
        "- **Persistent-agent bootloader:** Responsibility-specific goals, authority, risks, sources, and workflows.\n"
        "- **Thread plan or work state:** Live owned work.\n"
        "- **Task brief:** Temporary context for one bounded result.\n\n"
        "Your own plan's bounded status is delivered with context; parent and adjacent plans are not. Retrieve exact live detail through plan_read.\n\n"
        "## Shared work and context routes\n"
        "- _(Work or decision class, authoritative context, trigger or freshness rule, and verification or failure behavior.)_\n\n"
        "## Shared ownership and failure behavior\n"
        "- _(Project-wide ownership seam, approval boundary, or loud failure rule.)_\n\n"
        "Integrate authoritative sources when practical. For an unintegrated source, record its access trigger, owner, freshness rule, and failure behavior.\n\n"
        "Missing, stale, unreadable, or contradictory required context fails visibly; never substitute a convenient older source.\n\n"
        "## Conductor stewardship\n"
        "Review outcomes and context gaps, make the smallest durable improvement, verify delivery, and retire superseded policy. "
        "Do not rewrite the user's goals, mandate, commitments, or accepted risks without user direction.\n"
    )