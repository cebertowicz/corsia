"""Browser command adapters for the process owner."""
from __future__ import annotations

from typing import Any, Sequence


class BrowserProcessMixin:
    def browser_sessions(self) -> Any:
        raise NotImplementedError

    async def run_browser(
        self,
        thread_id: str,
        args: Sequence[str],
        *,
        lease_id: str | None = None,
        timeout: float = 30.0,
        show_window: bool | None = None,
        navigation_url: str | None = None,
    ) -> str:
        """Delegate one browser command to the composed session owner."""
        options = {"navigation_url": navigation_url} if navigation_url is not None else {}
        return await self.browser_sessions().run(
            thread_id, args, lease_id=lease_id, timeout=timeout, show_window=show_window, **options,
        )

    async def close_browser(self, thread_id: str) -> str:
        """Close only the composed browser session for one exact thread."""
        return await self.browser_sessions().close_thread(thread_id)
