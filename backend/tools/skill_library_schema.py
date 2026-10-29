"""Agent-facing contract for the library tool."""

DESCRIPTION = (
    "Find reviewed skills and blueprints in the public CORSIA library. Check your own skills index first. "
    "Search takes query; install, update and remove take a skill name. "
    "Only the actual project conductor or workspace CORSIA agent may use fetch_pack with a blueprint name; "
    "it fetches the current index version into that agent's thread. If there is no conductor, continue without the pack. "
    "After fetching, read manifest.yaml at the returned path and propose to the person which parts to adopt, never all of it. "
    "Skills are installed in your persistent owner's thread; updates and withdrawals are reported, and copies are not changed without your action."
)


def parameters():
    """Return the library tool's agent-facing action schema."""
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["search", "install", "update", "remove", "fetch_pack"],
                       "description": "search skills and blueprints, manage a skill, or fetch a blueprint as the conductor or CORSIA agent"},
            "query": {"type": "string", "description": "search only: words describing the job to do"},
            "name": {"type": "string", "description": "exact name from a search result or your skill index"},
            "discard_local_edits": {"type": "boolean", "default": False,
                                    "description": "update/remove only: set true only with the local_edits_token returned after reviewing affected files"},
            "local_edits_token": {"type": "string", "description": "update/remove only: exact token returned by local_edits; it confirms the listed edits may be discarded"},
        },
        "required": ["action"],
    }
