"""Out-of-process frames in the password probe and read guard, over a fake CDP connection.

The real-Chrome case is tests/test_browser_frame_password_real.py; these cover the shapes it can't stage cheaply:
nested frames, a hidden frame element, an unreadable frame owner and a page with too many frames.
"""
import pytest

from host_access import browser_cdp, browser_control, browser_cdp_frames, browser_steps, browser_step_lifecycle as lifecycle
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError
from tests.test_browser_password_guard import clean_password_guard  # noqa: F401 - autouse fixture

SHOWN, HIDDEN = [0, 0, 10, 0, 10, 10, 0, 10], [0] * 8


def key(*path: int) -> int:
    """The probe's key for a node reached through frame elements: key(frame, frame, ..., node)."""
    owner = browser_cdp_frames.node_key(None, path[0])
    for node in path[1:]:
        owner = browser_cdp_frames.node_key(owner, node)
    return owner


class FramedPage:
    """One CDP connection over several documents, each a session: None is the top document.

    ``documents`` maps a session to its password inputs ({backendNodeId: shown}), other elements' boxes and its
    child frames [(session, frame element backendNodeId, element shown)]. Node ids restart in every document.
    """

    def __init__(self, documents):
        self.documents = documents
        self.session_id, self.parents, self.attached = None, {}, []
        self._session_parents = {}
        self.calls = []

    def __call__(self, _endpoint):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def session_parents(self, session_id):
        return self._session_parents.setdefault(session_id, {})

    async def call(self, method, **params):
        return await self.call_in(None, method, **params)

    async def call_in(self, session_id, method, **params):
        self.calls.append((session_id, method))
        doc = self.documents[session_id]
        frames = doc.get("frames", [])
        if method == "Target.setAutoAttach":
            self.attached += [{"sessionId": child, "targetInfo": {"type": "iframe", "targetId": f"target-{child}"},
                               "parentSessionId": session_id} for child, _, _ in frames]
            return {}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": next(owner for child, owner, _ in frames if params["frameId"] == f"target-{child}")}
        if method == "DOM.getSearchResults":
            return {"nodeIds": list(doc.get("passwords", {}))}
        if method == "DOM.describeNode":
            return {"node": {"nodeName": "INPUT", "nodeId": params["nodeId"], "backendNodeId": params["nodeId"],
                             "attributes": ["type", "password"]}}
        if method == "DOM.getBoxModel":
            shown = {**doc.get("passwords", {}), **doc.get("boxes", {}), **{owner: up for _, owner, up in frames}}
            return {"model": {"border": SHOWN if shown.get(params["backendNodeId"]) else HIDDEN}}
        return {
            "Page.getFrameTree": {"frameTree": {"frame": {"loaderId": "visit", "url": "https://shop.example/"}}},
            "DOM.getDocument": {"root": {"nodeId": 1}},
            "DOM.performSearch": {"searchId": "search", "resultCount": len(doc.get("passwords", {}))},
            "DOM.discardSearchResults": {},
        }[method]


async def _probe(monkeypatch, documents, known=None):
    page = FramedPage(documents)
    monkeypatch.setattr(browser_cdp, "CdpClient", page)
    return await browser_cdp.page_password_probe("ws://page.test", known or {}), page


@pytest.mark.asyncio
async def test_password_fields_in_cross_origin_and_nested_frames_are_found_under_their_frame_element(monkeypatch):
    # The same node id (5) in the page, a frame and the frame inside it names three different fields.
    probe, page = await _probe(monkeypatch, {
        None: {"passwords": {5: True}, "frames": [("outer", 10, True)]},
        "outer": {"passwords": {5: True}, "frames": [("inner", 11, True)]},
        "inner": {"passwords": {5: True}},
    })
    assert probe.visible_nodes == probe.present_nodes == frozenset({5, key(10, 5), key(10, 11, 5)})
    assert {browser_cdp_frames.frame_owner(node) for node in probe.visible_nodes} == {5, 10}
    # Each document is searched on its own session, and the search is discarded there.
    for session in (None, "outer", "inner"):
        assert (session, "DOM.performSearch") in page.calls and (session, "DOM.discardSearchResults") in page.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", ["outer", "inner"])
async def test_a_field_counts_as_visible_only_while_every_frame_element_above_it_shows(monkeypatch, hidden):
    probe, _ = await _probe(monkeypatch, {
        None: {"frames": [("outer", 10, hidden != "outer")]},
        "outer": {"passwords": {5: True}, "frames": [("inner", 11, hidden != "inner")]},
        "inner": {"passwords": {5: True}},
    })
    assert probe.present_nodes == frozenset({key(10, 5), key(10, 11, 5)})
    assert probe.visible_nodes == (frozenset() if hidden == "outer" else frozenset({key(10, 5)}))


@pytest.mark.asyncio
async def test_a_known_frame_field_stays_guarded_on_its_visit_after_it_stops_matching(monkeypatch):
    """A reveal toggle can turn the frame's field into a text input; the visit still counts it while it shows."""
    probe, _ = await _probe(monkeypatch, {None: {"frames": [("outer", 10, True)]}, "outer": {"boxes": {5: True}}},
                            known={"visit": frozenset({key(10, 5)}), "other-visit": frozenset({key(10, 6)})})
    assert probe.visible_nodes == frozenset({key(10, 5)}) and probe.present_nodes == frozenset()


@pytest.mark.asyncio
async def test_a_known_field_in_a_frame_that_went_away_is_not_reported(monkeypatch):
    probe, _ = await _probe(monkeypatch, {None: {}}, known={"visit": frozenset({key(10, 5)})})
    assert probe.visible_nodes == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [None, 0, -3, 1 << 32, "10", True])
async def test_a_frame_whose_element_cant_be_read_fails_the_page_check(monkeypatch, owner):
    with pytest.raises(BrowserSessionError, match="couldn't be checked"):
        await _probe(monkeypatch, {None: {"frames": [("outer", owner, True)]}, "outer": {}})


@pytest.mark.asyncio
async def test_a_page_with_more_frames_than_the_limit_fails_the_page_check(monkeypatch):
    frames = [(f"frame-{n}", n + 1, True) for n in range(browser_cdp_frames.FRAME_LIMIT)]
    documents = {None: {"frames": frames}, **{session: {} for session, _, _ in frames}}
    with pytest.raises(BrowserSessionError, match="couldn't be checked"):
        await _probe(monkeypatch, documents)
    # At the limit itself the page is checked.
    documents[None]["frames"] = frames[:-1]
    probe, _ = await _probe(monkeypatch, documents)
    assert probe.visible_nodes == frozenset()


@pytest.mark.asyncio
async def test_a_frame_attached_under_another_session_is_not_taken_for_a_child(monkeypatch):
    page = FramedPage({None: {}})
    monkeypatch.setattr(browser_cdp, "CdpClient", page)
    page.attached.append({"sessionId": "stray", "targetInfo": {"type": "iframe", "targetId": "target-stray"},
                          "parentSessionId": "elsewhere"})
    assert await browser_cdp_frames.child_frames(page, page) == []


def test_node_keys_round_trip_and_reject_ids_out_of_range():
    nested = key(10, 11, 5)
    assert browser_cdp_frames.split_key(nested) == (key(10, 11), 5)
    assert browser_cdp_frames.split_key(key(10, 5)) == (10, 5) and browser_cdp_frames.split_key(5) == (None, 5)
    assert browser_cdp_frames.frame_owner(nested) == 10 and browser_cdp_frames.frame_owner(5) == 5
    assert key(10, 5) != key(5, 10) != 5
    for node in (0, -1, 1 << 32, True, "5", None, 5.0):
        with pytest.raises(BrowserSessionError, match="couldn't be checked"):
            browser_cdp_frames.node_key(None, node)


@pytest.mark.asyncio
async def test_a_read_that_lands_on_a_guarded_fields_frame_element_is_refused(monkeypatch):
    """The hit test can only reach a cross-origin field through its frame element in the page, so that is refused."""
    field = key(10, 5)
    shown = PasswordProbe("visit", "shop.example", frozenset({field}), present_nodes=frozenset({field}))
    hidden = PasswordProbe("visit", "shop.example", frozenset(), present_nodes=frozenset({field}))
    responses = iter((shown,))

    async def probe(_known):
        return next(responses, hidden)

    async def open_step(thread_id, step):
        lifecycle._steps[thread_id] = step
        lifecycle._start_reprobe(thread_id, step)
        step.node_id = "password-step"

    async def settle_step(_thread_id, _step):
        pass

    monkeypatch.setattr(lifecycle, "_open_step", open_step)
    monkeypatch.setattr(lifecycle, "_settle_step", settle_step)
    monkeypatch.setattr(browser_steps, "publish_password_page", lambda *_args: None)
    with pytest.raises(BrowserSessionError, match="asks for a password"):
        await browser_control.require_page_access("thread", probe, ("snapshot",))
    await browser_control.require_page_access("thread", probe, ("snapshot",))

    # 10 is the frame's element; 5 is a different element in the page that shares the field's node id.
    located = {"@frame": 10, "@page-field": 5, "@link": 7}

    async def resolve(ref):
        return located[ref]

    for args in (("get", "value", "@frame"), ("get", "attr", "@frame", "value")):
        with pytest.raises(BrowserSessionError, match="has shown a password field"):
            await browser_control.require_page_access("thread", probe, args, resolve=resolve)
    for ref in ("@page-field", "@link"):
        await browser_control.require_page_access("thread", probe, ("get", "value", ref), resolve=resolve)
