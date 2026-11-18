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

    def __init__(self, hits, boxes, frame_tree, owners=None, covers=None, holds=()):
        self.hits, self.boxes, self.frame_tree, self.owners = hits, boxes, frame_tree, owners or {}
        self.covers = covers or {}
        # The frames (by world) whose document holds an element with the target's box, whatever covers it.
        self.holds = set(holds)
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
            return {"executionContextId": len(self.worlds)}
        if method == "Runtime.callFunctionOn" and "elementsFromPoint" in params["functionDeclaration"]:
            frame = self.worlds[params["executionContextId"] - 1]
            return {"result": {"type": "boolean", "value": frame in self.holds}}
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


def _page(*, hit=LINK, frames=(), owners=None, holds=()):
    """A page whose top-page element at the unmoved point is node 8 and, in a frame, at the moved one ``hit``."""
    return Target(
        {TOP_HIT: TOP, FRAMED_HIT: hit},
        {LINK: {"border": PLACED}, TOP: {"border": [0, 0, 800, 0, 800, 60, 0, 60]},
         COVER: {"border": [0, 90, 800, 90, 800, 400, 0, 400]}, OWNER: {"content": FRAME_CONTENT}},
        {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": frame}} for frame in frames]},
        owners=owners or {}, covers={COVER: "DIV", TOP: "HEADER"}, holds=holds)


def _frame(*, hit=LINK, holds=()):
    """A cross-site frame's own target: the same link, measured from the frame's corner."""
    return Target({TOP_HIT: hit}, {LINK: {"border": RAW}, COVER: {"border": [0, 0, 600, 0, 600, 300, 0, 300]}},
                  {"frame": {"id": "oopif"}}, covers={COVER: "DIV"}, holds=holds)


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


def _twins(*, twin_hit_node, holds):
    """Two frames laid out alike: the link at the first's place, and ``twin_hit_node`` at the second's."""
    twin_owner, twin_hit = 51, (56 + 50, 414 + 10)
    return Target(
        {TOP_HIT: TOP, FRAMED_HIT: LINK, twin_hit: twin_hit_node},
        {LINK: {"border": PLACED}, TOP: {"border": [0, 0, 800, 0, 800, 60, 0, 60]},
         COVER: {"border": [0, 380, 800, 380, 800, 600, 0, 600]}, OWNER: {"content": FRAME_CONTENT},
         twin_owner: {"content": [8, 380, 608, 380, 608, 580, 8, 580]}},
        {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "child"}}, {"frame": {"id": "twin"}}]},
        owners={"child": OWNER, "twin": twin_owner}, covers={TOP: "HEADER", COVER: "DIV"}, holds=holds)


@pytest.mark.asyncio
async def test_a_twin_frames_covered_match_still_makes_the_ref_ambiguous_and_nothing_is_written(monkeypatch):
    """The ref may name the second frame's link, under an overlay: the first frame's uncovered one is not assumed."""
    page = _twins(twin_hit_node=COVER, holds={"twin"})
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused, match="more than one frame") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "ambiguous" and page.written == []


def _twin_off_screen(content, *, holds):
    """Two frames laid out alike, the second wholly outside the page's 800x600 view at ``content``."""
    twin_owner = 51
    return Target(
        {TOP_HIT: TOP, FRAMED_HIT: LINK},
        {LINK: {"border": PLACED}, TOP: {"border": [0, 0, 800, 0, 800, 60, 0, 60]}, OWNER: {"content": FRAME_CONTENT},
         twin_owner: {"content": content}},
        {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "child"}}, {"frame": {"id": "twin"}}]},
        owners={"child": OWNER, "twin": twin_owner}, covers={TOP: "HEADER"}, holds=holds)


BELOW_THE_FOLD = [8, 2000, 608, 2000, 608, 2300, 8, 2300]
PAST_THE_RIGHT_EDGE = [1200, 68, 1800, 68, 1800, 368, 1200, 368]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [BELOW_THE_FOLD, PAST_THE_RIGHT_EDGE], ids=["below-the-fold", "past-the-right-edge"])
async def test_a_look_alike_frame_off_screen_still_makes_the_ref_ambiguous_and_nothing_is_written(monkeypatch, content):
    """The ref may name the off-screen embed's link: the on-screen twin's match is not assumed to be the one."""
    page = _twin_off_screen(content, holds={"child", "twin"})
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused, match="more than one frame") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "ambiguous" and page.written == []


@pytest.mark.asyncio
async def test_a_frame_below_the_fold_that_keeps_moving_does_not_stop_a_write_to_the_frame_in_view(monkeypatch):
    """A carousel or ad below the fold moves on every read; only the frame in view must hold still."""
    page = _twin_off_screen(BELOW_THE_FOLD, holds={"child"})
    moves = iter(range(0, 10_000, 7))

    def moving_twin():
        y = 2000 + next(moves)
        return {"content": [8, y, 608, y, 608, y + 300, 8, y + 300]}

    boxes = page.boxes
    page.boxes = type("Boxes", (dict,), {"get": lambda self, key: moving_twin() if key == 51 else boxes.get(key)})()
    _connect(monkeypatch, **{PAGE_URL: page})
    assert await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2")) == "ok"
    assert page.written == ["field"]


class _GoneFrame:
    """A cross-site frame's target that went away between being listed and being opened."""

    async def __aenter__(self):
        raise OSError("connection refused")

    async def __aexit__(self, *_args):
        pass


@pytest.mark.asyncio
async def test_a_cross_site_frame_off_screen_that_cannot_be_opened_counts_as_holding_it_so_nothing_is_written(
        monkeypatch):
    """A doubt refuses: the frame that went away may have held the element the agent named."""
    page = _twin_off_screen(BELOW_THE_FOLD, holds={"child"})
    page.frame_tree = {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "child"}}]}
    monkeypatch.setattr(browser_dom_frames, "frame_targets", lambda _endpoint: {"twin": FRAME_URL})
    _connect(monkeypatch, **{PAGE_URL: page, FRAME_URL: _GoneFrame()})
    with pytest.raises(browser_dom_write.DomWriteRefused, match="more than one frame") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "ambiguous" and page.written == []


@pytest.mark.asyncio
async def test_an_off_screen_frame_that_alone_holds_the_element_keeps_the_pages_own_refusal(monkeypatch):
    """Nothing on screen holds the element and the off-screen frame can't be hit: refused as before frames were searched."""
    page = _twin_off_screen(BELOW_THE_FOLD, holds={"twin"})
    page.hits[FRAMED_HIT] = TOP
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused, match=r"another element covers it \(a <header>\)") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "covered" and page.written == []


@pytest.mark.asyncio
async def test_a_page_element_under_a_banner_with_a_look_alike_in_a_frame_is_ambiguous_not_written_in_the_frame(
        monkeypatch):
    """The ref may name the page's own element under its sticky header: the frame's look-alike is not assumed."""
    page = _page(frames=("child",), owners={"child": OWNER}, holds={"main"})
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused, match="more than one frame") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "ambiguous" and page.written == []


@pytest.mark.asyncio
async def test_a_covered_box_in_a_frame_that_holds_no_such_element_keeps_the_pages_own_refusal(monkeypatch):
    """Only a frame that holds the element makes it a frame target; otherwise the page's cover is named."""
    page = _page(hit=COVER, frames=("child",), owners={"child": OWNER})
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused, match=r"another element covers it \(a <header>\)") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "covered" and page.written == []


@pytest.mark.asyncio
async def test_an_element_covered_inside_a_same_origin_frame_is_still_refused_naming_the_cover(monkeypatch):
    page = _page(hit=COVER, frames=("child",), owners={"child": OWNER}, holds={"child"})
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
    assert frame.resolved == [LINK] and frame.written == ["field"] and frame.worlds[-1] == "oopif"
    assert LINK not in page.resolved and page.written == []
    # The frame's target measures from its own corner, so the field is moved onto the page.
    assert marked == [({"x": 56, "y": 102, "width": 100, "height": 20}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay_on_page", [False, True])
async def test_an_element_covered_inside_a_cross_site_frame_is_still_refused(monkeypatch, overlay_on_page):
    monkeypatch.setattr(browser_dom_frames, "frame_targets", lambda _endpoint: {"oopif": FRAME_URL})
    # Either the frame's own page covers the link, or something on the top page covers the whole frame.
    page = _page(hit=COVER if overlay_on_page else OWNER, owners={"oopif": OWNER})
    frame = _frame(hit=COVER, holds={"oopif"})
    opened = _connect(monkeypatch, **{PAGE_URL: page, FRAME_URL: frame})
    with pytest.raises(browser_dom_write.DomWriteRefused, match=r"another element covers it \(a <div>\)") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "covered" and frame.written == [] and page.written == []
    # The frame's own target is read for whether it holds the link, once per write, though the page is read twice.
    assert opened.count(FRAME_URL) == 1


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
