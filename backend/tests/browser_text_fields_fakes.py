"""Fakes shared by the text-field box tests."""
from host_access.browser_session_state import BrowserSessionError

# Layout metrics for a page at its top, with a viewport larger than any pane the tests report.
UNSCROLLED = {'cssVisualViewport': {'pageX': 0, 'pageY': 0, 'clientWidth': 10_000, 'clientHeight': 10_000}}


class BatchingFake:
    """A fake page connection: `call` is the test's; a batch answers each entry through it, None where it fails."""

    session_id = None
    parents: dict[int, int] = {}

    async def call(self, method, **params):
        raise AssertionError(method)

    async def call_many(self, _session_id, method, params):
        results = []
        for one in params:
            try:
                results.append(await self.call(method, **one))
            except BrowserSessionError:
                results.append(None)
        return results
