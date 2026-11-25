"""Microsoft tool pack registration."""
from __future__ import annotations


def register(agent: object) -> None:
    from tools.microsoft_account import MicrosoftConnectTool, MicrosoftStatusTool
    from tools.microsoft_actions import (
        MicrosoftCalendarListTool, MicrosoftFilesListTool, MicrosoftFilesReadTool, MicrosoftFilesUploadTool,
        MicrosoftMailDraftTool, MicrosoftMailReadTool, MicrosoftMailSearchTool,
    )
    from tools.microsoft_calendar_invites import (
        MicrosoftCalendarCreateTool, MicrosoftCalendarDraftTool, MicrosoftCalendarUpdateTool,
    )
    for tool_type in (
        MicrosoftConnectTool, MicrosoftStatusTool, MicrosoftMailDraftTool, MicrosoftMailSearchTool, MicrosoftMailReadTool,
        MicrosoftCalendarListTool, MicrosoftCalendarDraftTool, MicrosoftCalendarCreateTool,
        MicrosoftCalendarUpdateTool, MicrosoftFilesListTool, MicrosoftFilesReadTool, MicrosoftFilesUploadTool,
    ):
        agent.tools.register(tool_type())  # type: ignore[attr-defined]
