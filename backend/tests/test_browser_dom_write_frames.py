"""A write on an element inside a frame: agent-browser measures it from the frame's corner, so it is moved onto the page."""
import copy
import json

import pytest

from host_access import browser_dom_frames, browser_dom_hit, browser_dom_write
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
        self.hold_reads = 0
        self.session_id = None
        self.parents = {}
        self.attached = []
        self.targets = {}
        self.root = self
        self.attach_enabled = False

    def session_parents(self, _session_id):
        return {}

    async def call_in(self, session_id, method, **params):
        return await self.targets[session_id].call(method, **params)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def call(self, method, **params):
        self.calls.append(method)
        if method == "Target.setAutoAttach":
            if not self.attach_enabled:
                self.attach_enabled = True
                for frame_id in self.owners:
                    if frame_id in self.root.targets:
                        self.root.attached.append({"sessionId": frame_id, "parentSessionId": self.session_id,
                                                   "targetInfo": {"type": "iframe", "targetId": frame_id}})
            return {}
        if method == "Page.getLayoutMetrics":
            return {"cssVisualViewport": {"clientWidth": 800, "clientHeight": 600, "pageX": 0, "pageY": 0}}
        if method == "Page.getFrameTree":
            tree = copy.deepcopy(self.frame_tree)
            pending = [tree]
            while pending:
                current = pending.pop()
                current["frame"].setdefault("loaderId", "fixture-loader")
                pending.extend(current.get("childFrames", []))
            return {"frameTree": tree}
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
        if method == "Runtime.callFunctionOn" and "querySelectorAll" in params["functionDeclaration"]:
            self.hold_reads += 1
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
    monkeypatch.setattr(browser_dom_hit, "SETTLE_INTERVAL", 0)


def _connect(monkeypatch, **targets):
    opened = []

    def client(endpoint):
        opened.append(endpoint)
        return targets[endpoint]

    page = targets[PAGE_URL]
    for endpoint, target in targets.items():
        if endpoint == PAGE_URL:
            continue
        if isinstance(target, _GoneFrame):
            frame_id = "twin"
            target = Target({}, {}, {"frame": {"id": frame_id}})
            async def gone(_method, **_params):
                raise OSError("frame session disconnected")
            target.call = gone
        else:
            frame_id = target.frame_tree["frame"]["id"]
        target.session_id = frame_id
        target.root = page
        page.targets[frame_id] = target
    monkeypatch.setattr(browser_dom_write, "CdpClient", client)
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
# On screen but 30px tall, so the box (centre 44px down) sits below the frame's own fold, in content it scrolls.
SHORT_FRAME = [8, 400, 608, 400, 608, 430, 8, 430]


@pytest.mark.asyncio
async def test_a_look_alike_frame_holding_the_element_below_its_own_fold_still_makes_the_ref_ambiguous(monkeypatch):
    """The ref may name the short frame's link, scrolled out of that frame's view: the other frame's is not assumed."""
    page = _twin_off_screen(SHORT_FRAME, holds={"child", "twin"})
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused, match="more than one frame") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "ambiguous" and page.written == []


@pytest.mark.asyncio
async def test_a_short_frame_that_holds_no_such_element_does_not_stop_a_write_to_the_frame_that_shows_it(monkeypatch):
    page = _twin_off_screen(SHORT_FRAME, holds={"child"})
    _connect(monkeypatch, **{PAGE_URL: page})
    assert await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2")) == "ok"
    assert page.written == ["field"]


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
    """A cross-site frame session that cannot be read."""

    async def __aenter__(self):
        raise OSError("connection refused")

    async def __aexit__(self, *_args):
        pass


@pytest.mark.asyncio
async def test_a_cross_site_frame_that_cannot_be_opened_refuses_the_write(
        monkeypatch):
    """A doubt refuses: the frame that went away may have held the element the agent named."""
    page = _twin_off_screen(BELOW_THE_FOLD, holds={"child"})
    page.frame_tree = {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "child"}}]}
    _connect(monkeypatch, **{PAGE_URL: page, FRAME_URL: _GoneFrame()})
    with pytest.raises(browser_dom_write.DomWriteRefused, match="page refused the change") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert str(refused.value.__cause__) == "This page couldn't be checked"
    assert refused.value.reason == "page_error" and page.written == []


DEEP_URL = "ws://deep.test/devtools/page/deep"


def _wrapped(monkeypatch, *, inner_holds, deep_holds=None):
    """The page's own frame holds the element; a cross-site wrapper below the fold holds a frame of its own.

    The wrapper's frame is in the wrapper's process (``inner``), or, with ``deep_holds``, a target of its own
    (``deep``) whose owner element is in the wrapper, not in the page.
    """
    page = _twin_off_screen(BELOW_THE_FOLD, holds={"child"})
    page.frame_tree = {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "child"}}]}
    wrapper = Target({}, {60: {"content": FRAME_CONTENT}, 61: {"content": FRAME_CONTENT}},
                     {"frame": {"id": "twin"}, "childFrames": [{"frame": {"id": "inner"}}]},
                     owners={"deep": 60, "inner": 61}, holds={"inner"} if inner_holds else ())
    targets = {PAGE_URL: page, FRAME_URL: wrapper}
    if deep_holds is not None:
        targets[DEEP_URL] = Target({}, {}, {"frame": {"id": "deep"}}, holds={"deep"} if deep_holds else ())
    _connect(monkeypatch, **targets)
    return page


@pytest.mark.asyncio
@pytest.mark.parametrize(("inner_holds", "deep_holds"), [(True, None), (False, True)], ids=["in-wrapper", "own-target"])
async def test_a_look_alike_nested_in_a_cross_site_frame_makes_the_ref_ambiguous(monkeypatch, inner_holds, deep_holds):
    """The ref may name the nested embed's link: the page's own frame's look-alike is not assumed to be the one."""
    page = _wrapped(monkeypatch, inner_holds=inner_holds, deep_holds=deep_holds)
    with pytest.raises(browser_dom_write.DomWriteRefused, match="more than one frame") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "ambiguous" and page.written == []


@pytest.mark.asyncio
async def test_frames_nested_in_a_cross_site_frame_that_hold_no_such_element_do_not_stop_the_write(monkeypatch):
    page = _wrapped(monkeypatch, inner_holds=False, deep_holds=False)
    assert await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2")) == "ok"
    assert page.written == ["field"]


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
    # The frame attaches through the page's session.
    page = _page(hit=OWNER, owners={"oopif": OWNER})
    frame = _frame()
    opened = _connect(monkeypatch, **{PAGE_URL: page, FRAME_URL: frame})
    marked = []
    assert await browser_dom_write.shared_action(_run, PAGE_URL, ("fill", "@e2", "text"),
                                                 lambda field, label: marked.append((field, label))) == "ok"
    assert opened == [PAGE_URL] and frame.hold_reads == 1
    assert frame.resolved == [LINK] and frame.written == ["field"] and frame.worlds[-1] == "oopif"
    assert LINK not in page.resolved and page.written == []
    # The frame's target measures from its own corner, so the field is moved onto the page.
    assert marked == [({"x": 56, "y": 102, "width": 100, "height": 20}, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("overlay_on_page", [False, True])
async def test_an_element_covered_inside_a_cross_site_frame_is_still_refused(monkeypatch, overlay_on_page):
    # The frame attaches through the page's session.
    # Either the frame's own page covers the link, or something on the top page covers the whole frame.
    page = _page(hit=COVER if overlay_on_page else OWNER, owners={"oopif": OWNER})
    frame = _frame(hit=COVER, holds={"oopif"})
    opened = _connect(monkeypatch, **{PAGE_URL: page, FRAME_URL: frame})
    with pytest.raises(browser_dom_write.DomWriteRefused, match=r"another element covers it \(a <div>\)") as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "covered" and frame.written == [] and page.written == []
    # The frame's own target is read for whether it holds the link, once per write, though the page is read twice.
    assert opened == [PAGE_URL] and frame.hold_reads == 1


@pytest.mark.asyncio
async def test_a_target_the_page_itself_holds_never_looks_for_frames(monkeypatch):
    page = Target({TOP_HIT: LINK}, {LINK: {"border": RAW}}, {"frame": {"id": "main"}})

    async def not_listed(_page):
        raise AssertionError("the page's own target does not list frames")

    monkeypatch.setattr(browser_dom_write, "walk_frames", not_listed)
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
async def test_attach_failure_refuses_even_with_a_same_process_match(monkeypatch):
    page = _page(frames=("child",), owners={"child": OWNER})
    original = page.call

    async def fail_attach(method, **params):
        if method == "Target.setAutoAttach":
            raise BrowserSessionError("This page couldn't be checked")
        return await original(method, **params)

    page.call = fail_attach
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused) as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "page_error" and page.written == []
    assert str(refused.value.__cause__) == "This page couldn't be checked"


@pytest.mark.asyncio
async def test_exactly_the_frame_limit_is_allowed():
    frames = tuple(f"frame-{number}" for number in range(browser_dom_frames.browser_cdp_frames.FRAME_LIMIT - 1))
    page = _page(frames=frames, owners={name: OWNER for name in frames})
    walk = await browser_dom_frames.walk_frames(page)
    assert len(walk.frames) + 1 == browser_dom_frames.browser_cdp_frames.FRAME_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["same-process", "attachment"])
async def test_frame_arriving_after_the_walk_refuses_without_writing(monkeypatch, kind):
    page = _page()
    _connect(monkeypatch, **{PAGE_URL: page})

    async def run(args, timeout):
        if args[0] == "scrollintoview":
            if kind == "same-process":
                page.frame_tree["childFrames"] = [{"frame": {"id": "late"}}]
            else:
                page.attached.append({"sessionId": "late", "parentSessionId": None,
                                      "targetInfo": {"type": "iframe", "targetId": "late"}})
        return await _run(args, timeout)

    with pytest.raises(browser_dom_write.DomWriteRefused) as refused:
        await browser_dom_write.shared_action(run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "page_error" and page.written == []


@pytest.mark.asyncio
async def test_more_than_the_frame_limit_refuses_without_writing(monkeypatch):
    frames = tuple(f"frame-{number}" for number in range(browser_dom_frames.browser_cdp_frames.FRAME_LIMIT))
    page = _page(frames=frames, owners={name: OWNER for name in frames})
    _connect(monkeypatch, **{PAGE_URL: page})
    with pytest.raises(browser_dom_write.DomWriteRefused) as refused:
        await browser_dom_write.shared_action(_run, PAGE_URL, ("click", "@e2"))
    assert refused.value.reason == "page_error" and page.written == []
    assert str(refused.value.__cause__) == "This page couldn't be checked"


@pytest.mark.asyncio
async def test_a_read_target_in_a_frame_is_not_located_so_the_password_guard_keeps_refusing_it(monkeypatch):
    page = _page(frames=("child",), owners={"child": OWNER})
    _connect(monkeypatch, **{PAGE_URL: page})
    assert await browser_dom_write.target_node(_run, PAGE_URL, "@e2") is None
    assert "DOM.getFrameOwner" not in page.calls
