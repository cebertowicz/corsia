"""The lessons response must not wait on the disconnect watcher.

Starlette's ``Request.is_disconnected`` awaits ``receive()`` inside a cancelled
anyio scope. A task cancellation that lands while the watcher is inside that
call can be absorbed there, so the watcher keeps polling. If the handler then
awaited the watcher to finish, a finished report was held back until the
client left, and the browser showed a spinner that never ended.
"""

import asyncio

from api import measurement


class _AbsorbingRequest:
    """A request that never disconnects and absorbs the first cancellation it sees mid-poll."""

    def __init__(self) -> None:
        self.polling = asyncio.Event()
        self._absorbed = False

    async def is_disconnected(self) -> bool:
        self.polling.set()
        try:
            await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            if self._absorbed:
                raise
            self._absorbed = True
        return False


async def test_finished_report_is_returned_while_the_watcher_absorbs_its_cancellation(monkeypatch):
    request = _AbsorbingRequest()
    report = {"scope": "project", "rows": []}

    async def finished_report(**_kwargs):
        await request.polling.wait()
        return report

    monkeypatch.setattr(measurement, "report_for_identity", finished_report)

    handler = asyncio.create_task(measurement.measurement_lessons(
        request, project="p", agent="none", range="12w", lesson_id=None, version=None,
        cursor=None, preview=True, evidence=None, user={"id": "u"},
    ))
    try:
        done, _pending = await asyncio.wait({handler}, timeout=2)
        assert done, "the finished report was held back by the disconnect watcher"
        assert handler.result() == report
    finally:
        handler.cancel()
