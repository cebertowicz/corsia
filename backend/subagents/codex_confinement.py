"""Codex's version-pinned, bridge-only review-worker launch boundary.

0.162.1 exposes no full model-request tools readback in app-server. The
credential-free capture test checks the entire Responses tools array, including
hosted tools, on start and resume. Other versions must refuse until recaptured.
The harness process itself is not OS-sandboxed; paths are enforced by CORSIA.
"""
from __future__ import annotations

import asyncio

from infra.database import get_db
from infra.thread_confinement import ConfinementRecordDamaged, parse
from subagents.cc_confinement import ConfinementRefused
from tools.confined_reads import REVIEW_READ_TOOLS

CAPTURED_CODEX_VERSION = "codex-cli 0.162.1"
# Verified against rust-v0.162.1 core/config.schema.json and tools/spec_plan.rs.
# Environment tools independently enable apply_patch, even with shell disabled.
CONFIG = (
    # Prevent plugin startup sync's hard-coded GitHub/ChatGPT requests too.
    "features.plugins=false",
    "features.stable_environment_tools=false",
    "features.view_image=false",
    "features.deferred_executor=false",
    "features.token_budget=false",
    "features.current_time_reminder=false",
    "features.send_message_to_user_async=false",
    "features.tool_suggest=false",
    "features.tool_search=false",
)


def thread_context(thread_id: str | None, *, db=None) -> dict:
    """Load and validate the canonical launch restriction, never a session copy."""
    if not thread_id:
        return {}
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT confinement,confined_read_dirs FROM threads WHERE id=?", (thread_id,),
        ).fetchone()
        if row is None:
            raise ConfinementRefused("Codex destination thread no longer exists; no worker was launched.")
        context = dict(row)
        try:
            parse(context.get("confinement"), context.get("confined_read_dirs"))
        except ConfinementRecordDamaged as exc:
            raise ConfinementRefused(str(exc)) from exc
        return context
    finally:
        if db is None:
            connection.close()


def neutral_working_dir(thread_id: str) -> str:
    """Create the worker's empty launch directory outside staged content."""
    from infra.config import RUNTIME_DIR

    # Not the staged content, its ancestors, a repository, or the auth home.
    directory = RUNTIME_DIR / "codex-confined-cwd" / thread_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return str(directory.resolve())


def assert_tools(definitions) -> None:
    """Refuse any surface other than exactly the four bridge read functions."""
    names = []
    for definition in definitions:
        tool = definition.get("function", definition)
        if definition.get("type") != "function" or tool.get("name") not in REVIEW_READ_TOOLS:
            raise ConfinementRefused(
                "Confined Codex tool surface contains a native or unapproved tool; update CORSIA."
            )
        names.append(tool["name"])
    if len(names) != len(REVIEW_READ_TOOLS) or set(names) != REVIEW_READ_TOOLS:
        raise ConfinementRefused(
            "Confined Codex requires exactly read_file, grep, glob and list_dir; update CORSIA."
        )


def prepare_launch(session) -> None:
    """Bind persisted confinement to every ordinary or alternate launch path."""
    context = thread_context(session.corsia_thread_id)
    session.confined = context.get("confinement") is not None
    if not session.confined:
        return
    if session.control_only or session.fork_id is not None:
        raise ConfinementRefused("A confined Codex worker cannot run a control session or fork.")
    if session.sandbox_mode != "read-only" or session.approval_policy != "never":
        raise ConfinementRefused("Confined Codex requires read-only sandbox and never approvals.")
    assert_tools(session.dynamic_tools)
    session.working_dir = neutral_working_dir(session.corsia_thread_id)


async def verify_version(binary: str) -> None:
    """Admit only the binary version whose complete model request was captured."""
    process = await asyncio.create_subprocess_exec(
        binary, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    version = output.decode(errors="replace").strip()
    if process.returncode != 0 or version != CAPTURED_CODEX_VERSION:
        raise ConfinementRefused(
            f"Confined Codex is verified only on {CAPTURED_CODEX_VERSION}; "
            "update CORSIA for this Codex version. No worker was launched."
        )


def tool_surface(working_dir: str, session, thread_context=None):
    """Freeze the shared read projection, or validate the existing live handle."""
    from subagents.codex_runtime_errors import CodexPreparedContextStale

    if session is not None:
        registry = getattr(session, "tool_registry", None)
        if registry is None:
            raise CodexPreparedContextStale(
                "Codex app-server handle has no frozen CORSIA tool registry"
            )
        tools = tuple(getattr(session, "dynamic_tools", ()) or ())
        if (thread_context or {}).get("confinement") is not None:
            assert_tools(tools)
            if not getattr(session, "confined", False):
                raise CodexPreparedContextStale("Confined Codex requires a confined live handle")
        return registry, tools
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions

    registry = build_tool_registry(working_dir)
    return registry, dynamic_tool_definitions(registry, thread_context)


def config_args() -> list[str]:
    """Render pinned native-tool controls after all ordinary launch overrides."""
    return [part for value in CONFIG for part in ("-c", value)]
