"""Microsoft tool pack registration."""
from __future__ import annotations


def register(agent: object) -> None:
    from tools.microsoft_account import MicrosoftConnectTool, MicrosoftStatusTool
    from tools.microsoft_actions import (
        MicrosoftCalendarCreateTool, MicrosoftCalendarDraftTool, MicrosoftCalendarListTool, MicrosoftCalendarUpdateTool,
        MicrosoftFilesListTool, MicrosoftFilesReadTool, MicrosoftFilesUploadTool,
        MicrosoftMailDraftTool, MicrosoftMailReadTool, MicrosoftMailSearchTool, MicrosoftMailSendTool,
    )
    for tool_type in (
        MicrosoftConnectTool, MicrosoftStatusTool, MicrosoftMailDraftTool, MicrosoftMailSearchTool, MicrosoftMailReadTool,
        MicrosoftMailSendTool, MicrosoftCalendarListTool, MicrosoftCalendarDraftTool, MicrosoftCalendarCreateTool,
        MicrosoftCalendarUpdateTool, MicrosoftFilesListTool, MicrosoftFilesReadTool, MicrosoftFilesUploadTool,
    ):
        agent.tools.register(tool_type())  # type: ignore[attr-defined]
