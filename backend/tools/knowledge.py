"""Knowledge base tools — write and search structured agent memory."""

from __future__ import annotations

from typing import Any

from tools.base import Tool


class WriteKnowledgeTool(Tool):
    """Write a structured fact to the knowledge base."""

    @property
    def name(self) -> str:
        return "write_knowledge"

    @property
    def description(self) -> str:
        return (
            "Write a structured fact, entity, decision, or preference to the knowledge base.\n\n"
            "The knowledge base is a searchable filesystem store at workspace/memory/kb/.\n"
            "Use this to persist important facts that should survive across conversations.\n\n"
            "USE THIS when you need to:\n"
            "- Record a fact about Steve, a project, or the system\n"
            "- Document a decision (what was decided and why)\n"
            "- Store a preference or style rule\n"
            "- Add an entity (person, tool, project) with structured info\n\n"
            "DO NOT use this for:\n"
            "- Ephemeral in-conversation scratchpad — the KB is durable, "
            "not a turn-scoped whiteboard.\n"
            "- Mid-conversation handoffs to future sessions — CTX and Documents "
            "own those future surfaces; the KB is not a handoff queue.\n\n"
            "Categories: facts | entities | decisions | preferences"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short descriptive title for this entry (used as filename slug)",
                },
                "body": {
                    "type": "string",
                    "description": "The content of this knowledge entry (markdown supported)",
                },
                "category": {
                    "type": "string",
                    "enum": ["facts", "entities", "decisions", "preferences"],
                    "description": "Category: facts=general facts, entities=named things, decisions=what+why, preferences=style rules",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for filtering (e.g. ['steve', 'timezone', 'personal'])",
                },
            },
            "required": ["title", "body", "category"],
        }

    async def execute(self, title: str, body: str, category: str, tags: list[str] | None = None, **kwargs: Any) -> str:
        from memory.knowledge_base import write_entry

        try:
            result = write_entry(title=title, body=body, category=category, tags=tags or [])
            action = result["action"]
            return (
                f"Knowledge entry {action} (id: {result['id']})\n"
                f"Category: {result['category']}\n"
                f"Path: {result['path']}"
            )
        except ValueError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing knowledge entry: {e}"


class SearchKnowledgeTool(Tool):
    """Search the knowledge base by keyword."""

    @property
    def name(self) -> str:
        return "search_knowledge"

    @property
    def description(self) -> str:
        return (
            "Search the knowledge base for facts, entities, decisions, or preferences.\n\n"
            "Uses keyword search across all entries in workspace/memory/kb/.\n"
            "Use 1-3 precise keywords that would appear in the entry text.\n\n"
            "USE THIS when you need to:\n"
            "- Look up a fact about Steve, a project, or the system\n"
            "- Find a past decision and its rationale\n"
            "- Check if a preference or rule has been recorded\n"
            "- Find information about a person, tool, or entity\n\n"
            "Returns matching entries with title, category, tags, and a snippet."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "1-3 keywords likely to appear in the entry text. Fewer precise words beat longer phrases.",
                },
                "category": {
                    "type": "string",
                    "enum": ["facts", "entities", "decisions", "preferences"],
                    "description": "Limit search to a specific category (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 10)",
                    "minimum": 1,
                    "maximum": 50,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, category: str | None = None, limit: int = 10, **kwargs: Any) -> str:
        from memory.knowledge_base import search_entries

        results = search_entries(query=query, category=category, limit=limit)

        if not results:
            scope = f" in category '{category}'" if category else ""
            return f"No knowledge entries found matching '{query}'{scope}"

        lines = [f"Found {len(results)} knowledge entry(ies) matching '{query}':\n"]
        for r in results:
            tags_str = f" [{', '.join(r['tags'])}]" if r.get("tags") else ""
            lines.append(f"**{r['title']}** ({r['category']}){tags_str}")
            if r.get("snippet"):
                lines.append(f"  {r['snippet']}")
            lines.append(f"  id: {r['id']} | updated: {r.get('updated_at', '')[:16]}")
            lines.append("")

        return "\n".join(lines)


class ListKnowledgeTool(Tool):
    """List all knowledge base entries."""

    @property
    def name(self) -> str:
        return "list_knowledge"

    @property
    def description(self) -> str:
        return (
            "List all entries in the knowledge base, optionally filtered by category.\n\n"
            "USE THIS when you need to:\n"
            "- See what's been recorded in the knowledge base\n"
            "- Browse entries by category\n"
            "- Get an overview of stored knowledge\n\n"
            "For searching by keyword, use search_knowledge instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["facts", "entities", "decisions", "preferences"],
                    "description": "Filter by category (optional — all categories if omitted)",
                },
            },
        }

    async def execute(self, category: str | None = None, **kwargs: Any) -> str:
        from memory.knowledge_base import list_entries

        entries = list_entries(category=category)

        if not entries:
            scope = f" in category '{category}'" if category else ""
            return f"No knowledge entries found{scope}. Use write_knowledge to add entries."

        by_cat: dict[str, list] = {}
        for e in entries:
            by_cat.setdefault(e["category"], []).append(e)

        lines = [f"{len(entries)} knowledge entry(ies):\n"]
        for cat, cat_entries in sorted(by_cat.items()):
            lines.append(f"### {cat} ({len(cat_entries)})")
            for e in cat_entries:
                tags_str = f" [{', '.join(e['tags'])}]" if e.get("tags") else ""
                lines.append(f"- **{e['title']}**{tags_str} (id: {e['id']})")
            lines.append("")

        return "\n".join(lines)
