"""A write on an element inside a frame: agent-browser measures it from the frame's corner, so it is moved onto the page."""
import json

import pytest

from host_access import browser_cdp, browser_dom_frames, browser_dom_hit, browser_dom_write
from host_access.browser_cli import CommandResult
from host_access.browser_session_state import BrowserSessionError

# The link inside the frame as agent-browser reports it (from the frame's corner), and the frame's content box
# on the page, so on the page the link is at (56, 102).
RAW_BOX = {"x": 48, "y": 34, "width": 100, "height": 20}
FRAME_CONTENT = [8, 68, 608, 68, 608, 368, 8, 368]
PLACED = [56, 102, 156, 102, 156, 122, 56, 122]
RAW = [48, 34, 148, 34, 148, 54, 48, 54]
TOP_HIT = (RAW_BOX["x"] + 50, RAW_BOX["y"] + 10)
FRAMED_HIT = (56 + 50, 102 + 10)
OWNER, LINK, TOP, COVER = 50, 7, 8, 9
PAGE_URL, FRAME_URL = "ws://page.test/devtools/page/1", "ws://frame.test/devtools/page/frame"


class Target:
    """One CDP target: the page, or a frame Chrome runs in its own process."""

    def __init__(self, hits, boxes, frame_tree, owners=None, covers=None):
        self.hits, self.boxes, self.frame_tree, self.owners = hits, boxes, frame_tree, owners or {}
        self.covers = covers or {}
        self.calls = []
        self.worlds = []
        self.resolved = []
        self.written = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def call(self, method, **params):
        self.calls.append(method)
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 800, "clientHeight": 600, "pageX": 0, "pageY": 0}}
        if method == "Page.getFrameTree":
            return {"frameTree": self.frame_tree}
        if method == "DOM.getFrameOwner":
            if params["frameId"] not in self.owners:
                raise BrowserSessionError("This page couldn't be checked")
            return {"backendNodeId": self.owners[params["frameId"]]}
        if method == "DOM.getNodeForLocation":
            hit = self.hits.get((params["x"], params["y"]))
            if hit is None:
                raise BrowserSessionError("This page couldn't be checked")
            return {"backendNodeId": hit}
        if method == "DOM.getBoxModel":
            quads = self.boxes.get(params.get("backendNodeId"))
            if quads is None:
                raise BrowserSessionError("This page couldn't be checked")
            return {"model": quads}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": params.get("backendNodeId"), "nodeName": self.covers.get(params.get("backendNodeId"))}}
        if method == "Page.createIsolatedWorld":
            self.worlds.append(params["frameId"])
            return {"executionContextId": 3}
        if method == "DOM.resolveNode":
            self.resolved.append(params["backendNodeId"])
            return {"object": {"objectId": "field"}}
        if method == "Runtime.callFunctionOn" and "labels" in params["functionDeclaration"]:
            return {"result": {"type": "object", "subtype": "null"}}
        if method == "Runtime.callFunctionOn" and "parentElement" in params["functionDeclaration"]:
            # Climbing from a hit to its parent finds none.
            return {"result": {"type": "object", "subtype": "null"}}
        if method == "Runtime.callFunctionOn":
            self.written.append(params["objectId"])
            return {"result": {"type": "string", "value": "ok"}}
        return {}


def _page(*, hit=LINK, frames=(), owners=None):
    """A page whose top-page element at the unmoved point is node 8 and, in a frame, at the moved one ``hit``."""
    return Target(
        {TOP_HIT: TOP, FRAMED_HIT: hit},
        {LINK: {"border": PLACED}, TOP: {"border": [0, 0, 800, 0, 800, 60, 0, 60]},
         COVER: {"border": [0, 90, 800, 90, 800, 400, 0, 400]}, OWNER: {"content": FRAME_CONTENT}},
        {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": frame}} for frame in frames]},
        owners=owners or {}, covers={COVER: "DIV", TOP: "HEADER"})


def _frame(*, hit=LINK):
    """A cross-site frame's own target: the same link, measured from the frame's corner."""
    return Target({TOP_HIT: hit}, {LINK: {"border": RAW}, COVER: {"border": [0, 0, 600, 0, 600, 300, 0, 300]}},
                  {"frame": {"id": "oopif"}}, covers={COVER: "DIV"})


async def _run(args, _timeout):
    if tuple(args[:2]) == ("get", "box"):
        return CommandResult(0, json.dumps({"data": RAW_BOX}), "")
    return CommandResult(0, "{}", "")


@pytest.fixture(autouse=True)
def _no_frame_targets(monkeypatch):
    monkeypatch.setattr(browser_dom_frames, "frame_targets", lambda _endpoint: {})
    monkeypatch.setattr(browser_dom_hit, "SETTLE_INTERVAL", 0)


def _connect(monkeypatch, **targets):
    opened = []

    def client(endpoint):
        opened.append(endpoint)
        return targets[endpoint]

    # The page is opened by the write path; a cross-site frame's own target by the frame search.
    monkeypatch.setattr(browser_dom_write, "CdpClient", client)
    monkeypatch.setattr(browser_dom_frames, "CdpClient", client)
    return opened


@pytest.mark.asyncio
async def test_a_link_in_a_same_origin_frame_is_hit_at_the_frames_place_and_written_in_the_frames_world(monkeypatch):
    page = _page(frames=("child",), owners={"child": OWNER})
    _connect(monkeypatch, **{PAGE_URL: page})
    marked = []
    assert await browser_dom_write.shared_action(_run, PAGE_URL, ("fill", "@e2", "text"),
                                                 lambda field, label: marked.append((field, label))) == "ok"
    # The page path's miss climbed from the top element in the main world; the write itself ran in the frame's.
    assert page.resolved[-1] == LINK and page.worlds[-1] == "child" and page.written == ["field"]
    # The page measured the field, so it is already in page pixels and is not moved a second time.
    assert marked == [({"x": 56, "y": 102, "width": 100, "height": 20}, None)]


@pytest.mark.asyncio
async def test_a_write_into_a_frame_does_not_scroll_the_persons_page_first(monkeypatch):
    """A target in a frame always misses the page's own hit test; that miss is not a cover to scroll clear."""
    page = _page(frames=("child",), owners={"child": OWNER})
    _connect(monkeypatch, **{PAGE_URL: page})
    commands = []

    async def run(args, timeout):
        commands.append(tuple(args))
        return await _run(args, timeout)

    assert await browser_dom_write.shared_action(run, PAGE_URL, ("click", "@e2")) == "ok"
    assert page.written == ["field"] and not [command for command in commands if command[0] == "scrollintoview"]


@pytest.mark.asyncio
async def test_a_ref_that_two_frames_laid_out_alike_both_match_is_refused_and_nothing_is_written(monkeypatch):
    """Two embeds of the same form: the frame-relative box matches an element in each, so which was named is unknown."""
    twin_owner, twin_link = 51, 11
    twin_hit = (56 + 50, 414 + 10)
    page = Target(
        {TOP_HIT: TOP, FRAMED_HIT: LINK, twin_hit: twin_link},
        {LINK: {"border": PLACED}, twin_link: {"border": [56, 414, 156, 414, 156, 434, 56, 434]},
         TOP: {"border": [0, 0, 800, 0, 800, 60, 0, 60]}, OWNER: {"content": FRAME_CONTENT},
         twin_owner: {"content": [8, 380, 608, 380, 608, 580, 8, 580]}},
        {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "child"}}, {"frame": {"id": "twin"}}]},
        owners={"child": OWNER, "twin": twin_owner}, covers={TOP: "HEADER"})
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused, match="more than one frame") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "ambiguous" and page.written == []


@pytest.mark.asyncio
async def test_an_element_covered_inside_a_same_origin_frame_is_still_refused_naming_the_cover(monkeypatch):
    page = _page(hit=COVER, frames=("child",), owners={"child": OWNER})
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused, match=r"another element covers it \(a <div>\)") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    # The tag is the framed cover's, not the top page's element at the unmoved point (a <header>).
    assert refused.value.reason == "covered" and page.written == []


@pytest.mark.asyncio
async def test_a_link_in_a_cross_site_frame_is_hit_in_the_frames_own_target_and_written_there(monkeypatch):
    monkeypatch.setattr(browser_dom_frames, "frame_targets", lambda _endpoint: {"oopif": FRAME_URL})
    page = _page(hit=OWNER, owners={"oopif": OWNER})
    frame = _frame()
    opened = _connect(monkeypatch, **{PAGE_URL: page, FRAME_URL: frame})
    marked = []
    assert await browser_dom_write.shared_action(_run, PAGE_URL, ("fill", "@e2", "text"),
                                                 lambda field, label: marked.append((field, label))) == "ok"
    assert opened == [PAGE_URL, FRAME_URL]
    assert frame.resolved == [LINK] and frame.written == ["field"] and frame.worlds == ["oopif"]
    assert LINK not in page.resolved and page.written == []
    # The frame's target measures from its own corner, so the field is moved onto the page.
    assert marked == [({"x": 56, "y": 102, "width": 100, "height": 20}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay_on_page", [False, True])
async def test_an_element_covered_inside_a_cross_site_frame_is_still_refused(monkeypatch, overlay_on_page):
    monkeypatch.setattr(browser_dom_frames, "frame_targets", lambda _endpoint: {"oopif": FRAME_URL})
    # Either the frame's own page covers the link, or something on the top page covers the whole frame.
    page = _page(hit=COVER if overlay_on_page else OWNER, owners={"oopif": OWNER})
    frame = _frame(hit=COVER)
    opened = _connect(monkeypatch, **{PAGE_URL: page, FRAME_URL: frame})
    with pytest.raises(browser_dom_write.DomWriteRefused, match=r"another element covers it \(a <div>\)") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "covered" and frame.written == [] and page.written == []
    assert (FRAME_URL in opened) is (not overlay_on_page)


@pytest.mark.asyncio
async def test_a_target_the_page_itself_holds_never_looks_for_frames(monkeypatch):
    page = Target({TOP_HIT: LINK}, {LINK: {"border": RAW}}, {"frame": {"id": "main"}})

    def not_listed(_endpoint):
        raise AssertionError("the page's own target does not list frames")

    monkeypatch.setattr(browser_dom_frames, "frame_targets", not_listed)
    _connect(monkeypatch, **{PAGE_URL: page})
    assert await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2")) == "ok"
    assert page.worlds == ["main"] and "DOM.getFrameOwner" not in page.calls


@pytest.mark.asyncio
async def test_a_box_that_falls_outside_the_frame_is_not_looked_for_in_it(monkeypatch):
    """An element scrolled out of its frame's own view is not at the point a frame would put it on the page."""
    page = _page(frames=("child",), owners={"child": OWNER})
    _connect(monkeypatch, **{PAGE_URL: page})

    async def run(args, timeout):
        if tuple(args[:2]) == ("get", "box"):
            return CommandResult(0, json.dumps({"data": {**RAW_BOX, "y": 400}}), "")
        return await _run(args, timeout)

    with pytest.raises(browser_dom_write.DomWriteRefused) as refused:
        await browser_dom_write.shared_action(run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason != "ok" and page.written == [] and "DOM.resolveNode" not in page.calls


@pytest.mark.asyncio
async def test_a_read_target_in_a_frame_is_not_located_so_the_password_guard_keeps_refusing_it(monkeypatch):
    page = _page(frames=("child",), owners={"child": OWNER})
    _connect(monkeypatch, **{PAGE_URL: page})
    assert await browser_dom_write.target_node(_run, PAGE_URL, "@e2") is None
    assert "DOM.getFrameOwner" not in page.calls


def test_frame_targets_are_the_listed_iframe_targets_by_frame_id(monkeypatch):
    monkeypatch.undo()
    targets = [{"type": "page", "id": "p", "webSocketDebuggerUrl": "ws://x/page"},
               {"type": "iframe", "id": "oopif", "webSocketDebuggerUrl": "ws://x/frame"},
               {"type": "iframe", "webSocketDebuggerUrl": "ws://x/no-id"}]
    monkeypatch.setattr(browser_cdp, "list_targets", lambda _endpoint: targets)
    assert browser_dom_frames.frame_targets("ws://x/devtools/page/p") == {"oopif": "ws://x/frame"}

    def unreachable(_endpoint):
        raise BrowserSessionError("This page couldn't be checked")

    monkeypatch.setattr(browser_cdp, "list_targets", unreachable)
    assert browser_dom_frames.frame_targets("ws://x/devtools/page/p") == {}
