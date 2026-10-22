"""Agent initialization — CORSIAAgent setup and tool registration."""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from core.agent_context_sources import AgentRosterTool
from tools.agent_roster_update import AgentRosterEntryTool, AgentRosterUpdateTool
from tools.conductor_instruction_files import (
    ConductorInstructionFileReadTool,
    ConductorInstructionFileReplaceTool,
)
from infra import state
from infra.config import WORKSPACE
from infra.settings import get_agent_defaults, get_tool_config
from core.context_builder import ContextBuilder
from tools.base import Tool
from tools.registry import ToolRegistry


class CORSIAAgent:
    """CORSIA agent — manages tool registration, message history, and context building."""

    _TOOL_RESULT_MAX_CHARS = 500

    def __init__(
        self,
        workspace: Path,
        max_iterations: int = 200,
        memory_window: int = 150,
    ):
        self.workspace = workspace
        self.max_iterations = max_iterations
        self.memory_window = memory_window

        self.tools = ToolRegistry()
        self.context = ContextBuilder(workspace)

    async def _connect_mcp(self) -> None:
        """Attach the application-owned MCP tools before freezing native schemas."""
        from tools.mcp_runtime import register_mcp_tools

        register_mcp_tools(self.tools)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>...</think> blocks that some models embed in content."""
        from llm.think_tags import strip_think_tags
        return strip_think_tags(text)

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            val = next(iter(tc.arguments.values()), None) if tc.arguments else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}...")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)


def _register_continuity_tool(agent: CORSIAAgent) -> None:
    """Expose the one continuity editor on native and harness registries."""
    from tools.continuity import ContinuityEditTool

    agent.tools.register(ContinuityEditTool())


def _register_tool_surface(agent: CORSIAAgent) -> None:
    """Register the pack loader and the by-name dispatcher (``tools.tool_surface``)."""
    from tools.tool_surface import ToolCallTool, ToolsLoadTool

    agent.tools.register(ToolsLoadTool(agent.tools))
    agent.tools.register(ToolCallTool())


async def get_agent() -> CORSIAAgent:
    """Lazy-init the CORSIAAgent."""
    if state._agent is not None:
        return state._agent

    async with state._agent_lock:
        if state._agent is not None:
            return state._agent

        defaults = get_agent_defaults()
        tool_cfg = get_tool_config()

        workspace = WORKSPACE

        agent = CORSIAAgent(
            workspace=workspace,
            max_iterations=defaults.get("maxToolIterations", 200),
            memory_window=defaults.get("memoryWindow", 250),
        )

        _register_tools(agent, tool_cfg, workspace)
        _register_continuity_tool(agent)
        from tools.mcp_runtime import register_mcp_tools

        register_mcp_tools(agent.tools)
        from eval.harbor_tools import register_harbor_bridge_tools

        register_harbor_bridge_tools(agent.tools)
        _apply_eval_tool_allowlist(agent)

        state._agent = agent
        return agent


def _register_memory_tools(agent: CORSIAAgent) -> None:
    """Register the retrieval surface over durable memory.

    Grouped because these answer the same question from different layers:
    raw messages for exact wording, checkpoint generations for earlier
    trajectory, the thread transcript for an exact seq range, and user
    presence for whether the user has been here since the last reply.
    """
    from tools.query import (
        GetCheckpointsTool,
        GetThreadHistoryTool,
        SearchHistoryTool,
    )
    from tools.request_checkpoint import RequestCheckpointTool
    from tools.user_presence import UserPresenceTool
    from tools.post_update import PostUpdateTool
    from tools.lesson_report import LessonReportTool
    from tools.skill_library import SkillLibraryTool

    agent.tools.register(SearchHistoryTool())
    agent.tools.register(GetCheckpointsTool())
    agent.tools.register(RequestCheckpointTool())
    agent.tools.register(GetThreadHistoryTool())
    agent.tools.register(UserPresenceTool())
    agent.tools.register(PostUpdateTool())
    # Lesson evidence is scoped to the process-selected workspace, not a thread cwd.
    agent.tools.register(LessonReportTool(WORKSPACE))
    agent.tools.register(SkillLibraryTool())


def _register_project_creation_tools(agent: CORSIAAgent) -> None:
    """Register project creation and the consent-gated reads of the person's other tools.

    The registry is shared by every thread, so ``agent_import`` refuses at call
    time anyone but the CORSIA agent and a first-run thread.
    """
    from tools.agent_import import AgentImportTool
    from tools.project_discovery import ProjectDiscoveryListTool
    from tools.projects import CreateProjectTool

    agent.tools.register(CreateProjectTool())
    agent.tools.register(ProjectDiscoveryListTool())
    agent.tools.register(AgentImportTool())


def _register_access_tools(agent: CORSIAAgent) -> None:
    """Register lease controls, the scoped audit read and the conductor log read."""
    from tools.audit_reads import AuditReadTool
    from tools.host_access import ReleaseHostAccessTool, RequestHostAccessTool
    from tools.log_read import LogReadTool

    agent.tools.register(RequestHostAccessTool())
    agent.tools.register(ReleaseHostAccessTool())
    agent.tools.register(AuditReadTool())
    agent.tools.register(LogReadTool())


def _register_measure_tools(agent: CORSIAAgent) -> None:
    """Register the declared-measure tools: loading a measures.yaml and reading what intake did with it."""
    from tools.measures import MeasureStatusTool, MeasureSyncTool

    agent.tools.register(MeasureSyncTool())
    agent.tools.register(MeasureStatusTool())


def _register_automation_tools(agent: CORSIAAgent) -> None:
    """Register this thread's schedules and the event rules it writes."""
    from tools.automations import AutomationCancelTool, AutomationListTool, AutomationScheduleTool
    from tools.event_rules import EventRulesTool

    for tool in (AutomationScheduleTool(), AutomationCancelTool(), AutomationListTool(), EventRulesTool()):
        agent.tools.register(tool)


def _register_agent_roster_tools(agent: CORSIAAgent) -> None:
    """Register scope-aware roster reads and conductor-only tools."""
    from tools.bulletins import BulletinDispositionTool
    from tools.conductor_instruction_create import ConductorInstructionFileCreateTool
    from tools.corsia_api import CorsiaApiTool

    agent.tools.register(CorsiaApiTool())
    agent.tools.register(ConductorInstructionFileCreateTool())
    agent.tools.register(AgentRosterTool())
    agent.tools.register(AgentRosterEntryTool())
    agent.tools.register(AgentRosterUpdateTool())
    agent.tools.register(ConductorInstructionFileReadTool())
    agent.tools.register(ConductorInstructionFileReplaceTool())
    agent.tools.register(BulletinDispositionTool())


def _register_artifact_tools(agent: CORSIAAgent, workspace: Path) -> None:
    from tools.artifact_list import ArtifactListTool
    from tools.artifact_ownership import ArtifactReleaseTool, ArtifactRehomeTool
    from tools.artifacts import (
        ArtifactReconcileTool,
        ArtifactRegisterTool,
        ArtifactRelocateTool,
        ArtifactResolveTool,
        ArtifactUpdateTool,
    )

    agent.tools.register(ArtifactResolveTool(workspace=workspace))
    agent.tools.register(ArtifactListTool(workspace=workspace))
    agent.tools.register(ArtifactRegisterTool(workspace=workspace))
    agent.tools.register(ArtifactUpdateTool())
    agent.tools.register(ArtifactReconcileTool())
    agent.tools.register(ArtifactRelocateTool(workspace=workspace))
    agent.tools.register(ArtifactRehomeTool())
    agent.tools.register(ArtifactReleaseTool())


def _register_managed_process_tools(agent: CORSIAAgent) -> None:
    from tools.managed_processes import (
        ManagedProcessListTool,
        ManagedProcessLogsTool,
        ManagedProcessStatusTool,
        ManagedProcessStopTool,
        ProcessDiscoverTool,
        ProcessSignalTool,
    )

    for tool_type in (
        ManagedProcessStatusTool, ManagedProcessLogsTool, ManagedProcessListTool,
        ManagedProcessStopTool, ProcessDiscoverTool, ProcessSignalTool,
    ):
        agent.tools.register(tool_type())


def _register_delegation_tools(agent: CORSIAAgent) -> None:
    from tools.delegate_send import DelegateSendTool
    from tools.delegate_task import DelegateTaskTool
    from tools.delegation import DelegateCreateThreadTool, DelegateStatusTool
    from tools.delegation_model import DelegateSetModelTool
    from tools.model_catalog import ModelCatalogTool
    from tools.github_agent_account import GitHubAgentAccountTool
    from tools.model_sources import ModelSourcesTool
    from tools.register_microsoft import register as register_microsoft_tools

    for tool_type in (
        DelegateCreateThreadTool, DelegateSendTool, DelegateStatusTool,
        DelegateSetModelTool, DelegateTaskTool, ModelCatalogTool,
    ):
        agent.tools.register(tool_type())
    agent.tools.register(ModelSourcesTool())
    agent.tools.register(GitHubAgentAccountTool())
    _register_google_tools(agent)
    register_microsoft_tools(agent)


def _register_google_tools(agent: CORSIAAgent) -> None:
    from tools.google_account import GoogleConnectTool, GoogleStatusTool
    from tools.google_calendar import CalendarDraftInviteTool, CalendarListTool, CalendarSendInviteTool
    from tools.google_email import EmailDraftTool, EmailSendTool
    from tools.google_email_read import EmailReadTool, EmailSearchTool

    agent.tools.register(EmailSearchTool())
    agent.tools.register(EmailReadTool())
    agent.tools.register(GoogleConnectTool())
    agent.tools.register(GoogleStatusTool())
    agent.tools.register(EmailDraftTool())
    agent.tools.register(EmailSendTool())
    agent.tools.register(CalendarListTool())
    agent.tools.register(CalendarDraftInviteTool())
    agent.tools.register(CalendarSendInviteTool())


def _register_document_tools(agent: CORSIAAgent, workspace: Path) -> None:
    from tools.documents import (
        DocCommentCreateTool,
        DocCommentsListTool,
        DocCommentUpdateTool,
    )
    from tools.document_feedback import DocFeedbackSendTool
    from tools.document_open import DocumentOpenTool
    from tools.document_reviews import DocReviewRequestTool

    agent.tools.register(DocCommentsListTool(workspace=workspace))
    agent.tools.register(DocCommentCreateTool(workspace=workspace))
    agent.tools.register(DocCommentUpdateTool())
    agent.tools.register(DocFeedbackSendTool(workspace=workspace))
    agent.tools.register(DocumentOpenTool(workspace=workspace))
    agent.tools.register(DocReviewRequestTool(workspace=workspace))


def _register_tools(agent: CORSIAAgent, tool_cfg: dict, workspace: Path):
    """Register all tools on the agent."""
    from tools.filesystem import (
        EditFileTool,
        ListDirTool,
        MultiEditTool,
        ReadFileTool,
        WriteFileTool,
    )
    from tools.shell import BashTool
    from tools.web import WebSearchTool, WebFetchTool
    from tools.glob import GlobTool
    from tools.grep import GrepTool
    from tools.query import (
        ListProjectsTool, ProjectReposTool, SetConversationProjectTool,
    )
    from tools.project_repo_register import ProjectRepoRegisterTool
    allowed_dir = None if not tool_cfg.get("restrictToWorkspace") else workspace
    agent.tools.register(ReadFileTool(workspace=workspace, allowed_dir=allowed_dir))
    agent.tools.register(WriteFileTool(workspace=workspace, allowed_dir=allowed_dir))
    agent.tools.register(EditFileTool(workspace=workspace, allowed_dir=allowed_dir))
    agent.tools.register(MultiEditTool(workspace=workspace, allowed_dir=allowed_dir))
    agent.tools.register(ListDirTool(workspace=workspace, allowed_dir=allowed_dir))
    agent.tools.register(GlobTool(workspace=workspace, allowed_dir=allowed_dir))
    agent.tools.register(GrepTool(workspace=workspace, allowed_dir=allowed_dir))
    from tools.promote import PromoteArtifactTool
    agent.tools.register(PromoteArtifactTool(workspace=workspace))

    exec_cfg = tool_cfg.get("exec", {})
    bash_tool = BashTool(
        default_timeout=exec_cfg.get("timeout", 120),
        working_dir=str(workspace),
    )
    agent.tools.register(bash_tool)

    agent.tools.register(WebSearchTool())
    agent.tools.register(WebFetchTool())

    _register_memory_tools(agent)
    agent.tools.register(ListProjectsTool())
    agent.tools.register(ProjectReposTool())
    agent.tools.register(ProjectRepoRegisterTool())
    agent.tools.register(SetConversationProjectTool())
    _register_agent_roster_tools(agent)

    from tools.plans import (
        PlanDecisionTool,
        PlanEnsureTool,
        PlanReadTool,
        PlanUpdateTool,
    )
    from tools.tool_results import ReadToolResultTool
    from tools.worktrees import WorktreeCreateTool

    agent.tools.register(ReadToolResultTool())
    _register_project_creation_tools(agent)
    agent.tools.register(WorktreeCreateTool())
    agent.tools.register(PlanReadTool())
    agent.tools.register(PlanEnsureTool())
    agent.tools.register(PlanUpdateTool())
    agent.tools.register(PlanDecisionTool())
    _register_automation_tools(agent)

    _register_access_tools(agent)
    _register_measure_tools(agent)
    _register_managed_process_tools(agent)

    from tools.knowledge import WriteKnowledgeTool, SearchKnowledgeTool, ListKnowledgeTool
    agent.tools.register(WriteKnowledgeTool())
    agent.tools.register(SearchKnowledgeTool())
    agent.tools.register(ListKnowledgeTool())

    from tools.browser import BrowserOpenTool, BrowserScreenshotTool, BrowserCloseTool
    from tools.browser_actions import BrowserSnapshotTool, BrowserClickTool, BrowserFillTool, BrowserEvalTool, BrowserWaitTool
    from tools.browser_actions import BrowserPersonStepTool
    agent.tools.register(BrowserOpenTool(workspace=workspace))
    for tool in (BrowserSnapshotTool(), BrowserClickTool(), BrowserFillTool(), BrowserScreenshotTool(workspace=workspace),
                 BrowserEvalTool(), BrowserPersonStepTool(), BrowserWaitTool(), BrowserCloseTool()):
        agent.tools.register(tool)

    _register_document_tools(agent, workspace)

    _register_artifact_tools(agent, workspace)

    _register_delegation_tools(agent)

    from tools.restart import RestartResumeTool
    agent.tools.register(RestartResumeTool())
    _register_tool_surface(agent)


def _apply_eval_tool_allowlist(agent: CORSIAAgent) -> None:
    """Filter the whole native registry only for a disposable test instance."""
    from eval.tool_allowlist import eval_tool_allowlist

    allowlist = eval_tool_allowlist()
    if allowlist is not None:
        agent.tools.retain_only(allowlist)
