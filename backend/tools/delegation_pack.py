"""Registration of the delegation pack's tools on an agent."""
from __future__ import annotations


def register(agent) -> None:
    from tools.delegate_send import DelegateSendTool
    from tools.delegate_task import DelegateTaskTool
    from tools.delegation import DelegateCreateThreadTool, DelegateStatusTool
    from tools.delegation_model import DelegateSetModelTool
    from tools.delegation_stop import DelegateStopTool
    from tools.github_agent_account import GitHubAgentAccountTool
    from tools.model_catalog import ModelCatalogTool
    from tools.model_sources import ModelSourcesTool

    for tool_type in (
        DelegateCreateThreadTool, DelegateSendTool, DelegateStatusTool,
        DelegateSetModelTool, DelegateStopTool, DelegateTaskTool, ModelCatalogTool, ModelSourcesTool,
        GitHubAgentAccountTool,
    ):
        agent.tools.register(tool_type())
