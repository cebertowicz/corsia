"""The tool catalog's data: one declaration per registry tool.

``tools.catalog`` is the public import path; it re-exports these names with
the packs and lookups that read them.
"""

from __future__ import annotations

from tools.catalog_entry import ToolCatalogEntry
from tools.catalog_google_entries import GOOGLE_CATALOG_ENTRIES
from tools.catalog_host_entries import HOST_ACCESS_CATALOG_ENTRIES

_Entry = ToolCatalogEntry

POST_UPDATE_SUMMARY = "Leave the user a marked update they see in Focus mode"

POST_UPDATE_WORTH_SENDING = (
    "Send a direct answer to the user's message, a material assumption, a "
    "discovery, a phase result, a blocker, a changed expectation (commitment, "
    "cut-line or schedule change, recurring work stopping), or completion. "
    "Do not send tool-by-tool updates or a nothing-changed update."
)

POST_UPDATE_ATTENTION_GUIDANCE = (
    "Working steps fold while replies and marked messages show. "
    + POST_UPDATE_WORTH_SENDING
    + " Silence never answers; raise a plan decision for a choice."
)

TOOL_CATALOG: dict[str, ToolCatalogEntry] = {
    "continuity_edit": _Entry(
        "memory", status="substrate", authority="continuity-write", core=True,
        activity="Updating continuity",
    ),
    "read_file": _Entry("filesystem", core=True, activity="Reading"),
    "write_file": _Entry(
        "filesystem", authority="write-files", core=True, activity="Writing",
    ),
    "edit_file": _Entry("edit", authority="write-files", core=True, activity="Editing"),
    "multi_edit": _Entry(
        "edit", authority="write-files", core=True, activity="Editing",
    ),
    "list_dir": _Entry("filesystem", core=True, activity="Listing"),
    "glob": _Entry("filesystem", core=True, activity="Finding files"),
    "grep": _Entry("filesystem", core=True, activity="Searching"),
    "bash": _Entry("shell", authority="shell", core=True, activity="Running"),
    "tools_load": _Entry(
        "discovery", status="substrate", core=True, activity="Loading tools",
    ),
    "tool_call": _Entry(
        "discovery", status="substrate", core=True, activity="Calling tool",
    ),
    "web_search": _Entry("web", summary="Search the web", activity="Searching the web"),
    "web_fetch": _Entry(
        "web", summary="Fetch a URL as readable text", activity="Fetching",
    ),
    "search_history": _Entry(
        "history", summary="Keyword search across message history",
        activity="Searching history",
    ),
    "get_thread_history": _Entry(
        "history", summary="Read a thread's messages", activity="Reading history",
    ),
    "lesson_report": _Entry(
        "memory", summary="Read lesson versions, due reviews and recorded exposure",
        activity="Reading lesson evidence",
    ),
    "get_checkpoints": _Entry(
        "history", summary="Read past checkpoint conclusions",
        activity="Reading checkpoints",
    ),
    "request_checkpoint": _Entry(
        "history", summary="Fold 32,768+ tokens after a finished phase",
        activity="Requesting checkpoint",
    ),
    "bulletin_disposition": _Entry(
        "plans", status="substrate", authority="workspace-conductor",
        summary="Record a workspace conductor disposition for a System bulletin",
        activity="Recording bulletin disposition",
    ),
    "agent_roster": _Entry(
        "delegation", status="substrate",
        summary="List the active agents in this thread's scope",
        activity="Reading roster",
    ),
    "agent_roster_entry": _Entry(
        "delegation", status="active", authority="conductor-roster-read",
        summary="Read one scoped roster entry and its current revision",
        activity="Reading roster entry",
    ),
    "agent_roster_update": _Entry(
        "delegation", status="active", authority="conductor-roster-write",
        summary="Edit an agent roster entry in the current designated conductor scope",
        activity="Updating roster",
    ),
    "corsia_api": _Entry(
        "conductor", status="active", authority="conductor-api",
        summary="Call CORSIA API routes as a designated conductor, except the person-only list",
        activity="Calling CORSIA API",
    ),
    "conductor_instruction_file_create": _Entry(
        "conductor", status="active", authority="conductor-instruction-write",
        summary="Create and attach agent responsibility files or thread skills",
        activity="Installing instruction file",
    ),
    "conductor_instruction_file_read": _Entry(
        "conductor", status="active", authority="conductor-instruction-read",
        summary="List or read an agent's attached instruction file with its revision",
        activity="Reading instruction file",
    ),
    "conductor_instruction_file_replace": _Entry(
        "conductor", status="active", authority="conductor-instruction-write",
        summary="Replace one attached instruction file in the designated conductor scope",
        activity="Updating instruction file",
    ),
    "read_tool_result": _Entry(
        "history", summary="Page through a large earlier tool result",
        activity="Reading earlier result",
    ),
    "user_presence": _Entry(
        "history",
        summary=(
            "Is the user in this conversation now; have they been here since "
            "your last reply"
        ),
        activity="Checking presence",
    ),
    "post_update": _Entry(
        "history",
        summary=POST_UPDATE_SUMMARY,
        activity="Posting update",
    ),
    "project_list": _Entry(
        "projects", status="substrate", summary="List projects and their paths",
        activity="Listing projects",
    ),
    "project_repos": _Entry(
        "projects", status="substrate",
        summary="List this project's repositories",
        activity="Listing repositories",
    ),
    "project_repo_register": _Entry(
        "projects", status="substrate", authority="repository-register",
        summary="Register a Git repository with this project",
        activity="Registering repository",
    ),
    "conversation_set_project": _Entry(
        "projects", status="substrate", authority="project-scope",
        summary="Attach this conversation to a project, or detach it",
        activity="Setting project",
    ),
    "project_create": _Entry(
        "projects", status="substrate", authority="workspace-write",
        summary="Create a new project workspace",
        activity="Creating project",
    ),
    "project_discovery_list": _Entry(
        "projects", authority="consented-history-read",
        summary="List the folders the user works in, after their consent decision",
        activity="Finding projects",
    ),
    "worktree_create": _Entry(
        "projects", status="substrate", authority="repository-worktree-write",
        summary="Create a Git worktree and branch for a project repository",
        activity="Creating worktree",
    ),
    "write_knowledge": _Entry(
        "memory", status="suspect", authority="durable-memory-write",
        summary="Save a fact, entity, decision or preference",
        activity="Saving knowledge",
    ),
    "search_knowledge": _Entry(
        "memory", status="suspect", summary="Search the knowledge base",
        activity="Searching knowledge",
    ),
    "list_knowledge": _Entry(
        "memory", status="suspect", summary="List knowledge base entries",
        activity="Listing knowledge",
    ),
    "browser_open": _Entry(
        "browser", summary="Open a URL in the browser", activity="Opening",
    ),
    "browser_snapshot": _Entry(
        "browser", summary="Read the page's accessibility tree and element refs",
        activity="Reading page",
    ),
    "browser_click": _Entry(
        "browser", summary="Click a page element", activity="Clicking",
    ),
    "browser_fill": _Entry("browser", summary="Fill a text input", activity="Filling"),
    "browser_screenshot": _Entry(
        "browser", summary="Save a page screenshot", activity="Taking screenshot",
    ),
    "browser_eval": _Entry(
        "browser", authority="browser-script", summary="Run JavaScript in the page",
        activity="Running script",
    ),
    "browser_wait": _Entry(
        "browser", summary="Wait for an element or a delay", activity="Waiting",
    ),
    "browser_close": _Entry(
        "browser", summary="Close the browser session", activity="Closing browser",
    ),
    "delegate_create_thread": _Entry(
        "delegation", status="substrate",
        summary="Create a visible chat or a hidden subagent thread",
        activity="Creating thread",
    ),
    "delegate_send": _Entry(
        "delegation", status="substrate",
        summary="Send a message into another thread",
        activity="Messaging",
    ),
    "delegate_status": _Entry(
        "delegation", status="substrate",
        summary="Read a delegated thread's status and recent messages",
        activity="Checking agent",
    ),
    "delegate_set_model": _Entry(
        "delegation", status="substrate", authority="model-switch",
        summary="Change a running thread's model or harness in place",
        activity="Switching model",
    ),
    "delegate_task": _Entry(
        "delegation", status="substrate",
        summary="Delegate a self-contained task to a new subagent",
        activity="Delegating",
    ),
    "model_catalog": _Entry(
        "delegation", status="substrate",
        summary="Read the live model catalog before selecting a delegation target",
        activity="Reading model catalog",
    ),
    "model_sources": _Entry(
        "projects", status="substrate",
        summary="Read which model sources are ready, each with its fix, and GitHub",
        activity="Checking model sources",
    ),
    "github_agent_account": _Entry(
        "projects", status="substrate",
        summary="Connect and check the agents' own GitHub account",
        activity="Checking the agents' GitHub account",
    ),
    **GOOGLE_CATALOG_ENTRIES,
    "plan_read": _Entry(
        "plans", status="substrate", authority="plan-read", core=True,
        activity="Reading plan",
    ),
    "plan_ensure": _Entry(
        "plans", status="substrate", authority="plan-write",
        summary="Create this thread's plan if missing, then read it",
        activity="Opening plan",
    ),
    "plan_update": _Entry(
        "plans", status="substrate", authority="plan-write", core=True,
        activity="Updating plan",
    ),
    "plan_decision": _Entry(
        "plans", status="substrate", authority="plan-write",
        summary="Draft, review and raise a plan decision for the user",
        activity="Drafting decision",
    ),
    "automation_schedule": _Entry(
        "automations", status="substrate", authority="automation-write",
        summary="Schedule a clock or cadence wake for this thread",
        activity="Scheduling",
    ),
    "automation_cancel": _Entry(
        "automations", status="substrate", authority="automation-write",
        summary="Cancel one of this thread's schedules",
        activity="Cancelling automation",
    ),
    "automation_list": _Entry(
        "automations", status="substrate", authority="automation-read",
        summary="List this thread's schedules",
        activity="Listing automations",
    ),
    "restart_resume": _Entry(
        "delegation", status="substrate", authority="restart-continuity",
        summary="Resume this turn after the pending system restart",
        activity="Resuming after restart",
    ),
    "doc_comments_list": _Entry(
        "documents", status="substrate", summary="List a document's comments",
        activity="Reading comments",
    ),
    "doc_comment_create": _Entry(
        "documents", status="substrate", authority="document-comment-write",
        summary="Comment on, review or reply in a document",
        activity="Commenting",
    ),
    "doc_comment_update": _Entry(
        "documents", status="substrate", authority="document-comment-write",
        summary="Resolve, acknowledge or edit a document comment",
        activity="Updating comment",
    ),
    "doc_review_request": _Entry(
        "documents", status="substrate", authority="document-comment-write",
        summary="Ask an agent whose scope this document touches to review it",
        activity="Requesting review",
    ),
    "doc_feedback_send": _Entry(
        "documents", status="substrate", authority="document-comment-write",
        summary="Send or retry an agent's document feedback batches",
        activity="Sending document feedback",
    ),
    "document_open": _Entry(
        "documents", status="substrate",
        summary="Show a document to the user in this thread's canvas",
        activity="Opening document",
    ),
    "artifact_resolve": _Entry(
        "artifacts", status="substrate",
        summary="Look up an artifact by id, path or external id",
        activity="Looking up artifact",
    ),
    "artifact_list": _Entry(
        "artifacts", status="substrate",
        summary="List accessible shared documents by ownership and freshness",
        activity="Listing artifacts",
    ),
    "artifact_register": _Entry(
        "artifacts", status="substrate", authority="artifact-write",
        summary="Register a file or external object as an artifact",
        activity="Registering artifact",
    ),
    "artifact_update": _Entry(
        "artifacts", status="substrate", authority="artifact-write",
        summary="Change an artifact's description, visibility or pin",
        activity="Updating artifact",
    ),
    "artifact_reconcile": _Entry(
        "artifacts", status="substrate",
        summary="Preview or apply artifact pane repairs",
        activity="Reconciling artifacts",
    ),
    "artifact_relocate": _Entry(
        "artifacts", status="substrate", authority="artifact-write",
        summary="Move an artifact's file, keeping its identity",
        activity="Moving artifact",
    ),
    "promote_artifact": _Entry(
        "artifacts", status="substrate", authority="artifact-write",
        summary="Move a working file into shared/ and register it",
        activity="Promoting artifact",
    ),
    "artifact_rehome": _Entry(
        "artifacts", status="substrate", authority="artifact-ownership",
        summary="Transfer an artifact's ownership to another thread",
        activity="Transferring artifact",
    ),
    "artifact_release": _Entry(
        "artifacts", status="substrate", authority="artifact-ownership",
        summary="Release an artifact's ownership",
        activity="Releasing artifact",
    ),
    **HOST_ACCESS_CATALOG_ENTRIES,
}
