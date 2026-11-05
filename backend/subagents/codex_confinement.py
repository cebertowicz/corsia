"""Codex's version-pinned, bridge-only untrusted-worker launch boundary.

The credential-free capture test checked the entire Responses tools array,
including hosted tools, on start and resume for each listed version. Other
versions must refuse until captured.
The harness process itself is not OS-sandboxed; paths are enforced by CORSIA.
"""
from __future__ import annotations

import asyncio

from infra.database import get_db
from infra.thread_confinement import ConfinementRecordDamaged, read_dirs
from subagents.cc_confinement import ConfinementRefused
from subagents.codex_versions import CAPTURED_CODEX_VERSIONS, captured_versions
from tools.confined_reads import UNTRUSTED_READ_TOOLS

# Each listed version passed the full-array capture on start and resume.
# CONFIG keys appear in core/config.schema.json for every listed version;
# tools/spec_plan.rs and the capture cover tool planning and observed requests.
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


def confined_dirs(thread_id: str | None):
    if not thread_id:
        return None
    db = get_db()
    try:
        try:
            return read_dirs(thread_id, db)
        except ConfinementRecordDamaged as exc:
            raise ConfinementRefused(str(exc)) from exc
    finally:
        db.close()


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
        if definition.get("type") != "function" or tool.get("name") not in UNTRUSTED_READ_TOOLS:
            raise ConfinementRefused(
                "Couldn’t start this read-only agent on Codex: it couldn’t be limited to reading files. Choose another model."
            )
        names.append(tool["name"])
    if len(names) != len(UNTRUSTED_READ_TOOLS) or set(names) != UNTRUSTED_READ_TOOLS:
        raise ConfinementRefused(
            "Couldn’t start this read-only agent on Codex: it couldn’t be limited to reading files. Choose another model."
        )


def prepare_launch(session) -> None:
    """Bind persisted confinement to every ordinary or alternate launch path."""
    session.confined = confined_dirs(session.corsia_thread_id) is not None
    if not session.confined:
        return
    if session.fork_id is not None:
        from core.fork_types import ProviderForkRejected

        raise ProviderForkRejected("Couldn’t start this read-only agent on Codex: it can’t fork. Choose another model.")
    if session.control_only:
        raise ConfinementRefused("Couldn’t start this read-only agent on Codex: CORSIA can’t run this request. Choose another model.")
    if session.sandbox_mode != "read-only" or session.approval_policy != "never":
        raise ConfinementRefused("Couldn’t start this read-only agent on Codex: CORSIA couldn’t keep it read-only. Choose another model.")
    assert_tools(session.dynamic_tools)
    session.working_dir = neutral_working_dir(session.corsia_thread_id)


async def verify_version(binary: str | None = None) -> None:
    """Admit only the binary version whose complete model request was captured."""
    if binary is None:
        from infra.db_async import run_in_db
        from subagents.codex_runtime_config import codex_binary, codex_connection

        binary = await run_in_db(lambda: codex_binary(codex_connection()))
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
    if process.returncode != 0 or version not in CAPTURED_CODEX_VERSIONS:
        versions = captured_versions()
        supported = ", ".join(versions[:-1]) + " and " + versions[-1] if len(versions) > 1 else versions[0]
        raise ConfinementRefused(
            f"Couldn’t start this read-only agent on Codex {version.removeprefix('codex-cli ') or '(version unknown)'}: "
            f"read-only agents work only on Codex {supported}. "
            "Switch Codex to one of those, or choose another model."
        )


def tool_surface(working_dir: str, session, confined: bool = False):
    """Freeze the shared read projection, or validate the existing live handle."""
    from subagents.codex_runtime_errors import CodexPreparedContextStale

    if session is not None:
        registry = getattr(session, "tool_registry", None)
        if registry is None:
            raise CodexPreparedContextStale(
                "Codex app-server handle has no frozen CORSIA tool registry"
            )
        tools = tuple(getattr(session, "dynamic_tools", ()) or ())
        if confined:
            assert_tools(tools)
            if not getattr(session, "confined", False):
                raise CodexPreparedContextStale("Confined Codex requires a confined live handle")
        return registry, tools
    from subagents.harness_tool_bridge import build_tool_registry, dynamic_tool_definitions

    registry = build_tool_registry(working_dir)
    return registry, dynamic_tool_definitions(registry, confined)


def config_args() -> list[str]:
    """Render pinned native-tool controls after all ordinary launch overrides."""
    return [part for value in CONFIG for part in ("-c", value)]
