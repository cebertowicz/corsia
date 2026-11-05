"""Messages (iMessage) tool pack registration."""
from __future__ import annotations


def register(agent: object) -> None:
    from tools.messages_draft_tools import MessagesDraftTool, MessagesSendTool
    from tools.messages_tools import MessagesChatsTool, MessagesConnectTool, MessagesReadTool, MessagesSearchTool

    for tool_type in (
        MessagesConnectTool, MessagesChatsTool, MessagesReadTool, MessagesSearchTool, MessagesDraftTool, MessagesSendTool,
    ):
        agent.tools.register(tool_type())  # type: ignore[attr-defined]
