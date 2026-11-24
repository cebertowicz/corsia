"""Which agent commands mark a document, a site or a whole session against a saved-login fill (browser_fill_marks)."""
from __future__ import annotations

import pytest

from host_access import browser_cdp, browser_fill_marks as marks

THREAD = "thread-marks"


@pytest.fixture(autouse=True)
def _fresh():
    marks._scripted.clear()
    marks._session_marks.clear()
    marks._site_marks.clear()
    yield
    marks._scripted.clear()
    marks._session_marks.clear()
    marks._site_marks.clear()


@pytest.fixture
def on_page(monkeypatch):
    """The page's document and site as before_agent_command reads them; None stands for an unreadable page."""
    current = {"page": ("L1", "example.com")}

    async def page(_endpoint):
        return current["page"]

    monkeypatch.setattr(marks, "_page", page)
    return current


@pytest.mark.parametrize(("args", "document", "session"), [
    (("snapshot", "-i"), False, False),
    (("click", "@e2"), False, False),
    (("fill", "@e3", "text"), False, False),
    (("get", "text", "@e1"), False, False),
    (("wait", "--load", "networkidle"), False, False),
    (("eval", "document.title"), True, False),
    (("wait", "--fn", "window.ready"), True, False),
    (("get", "text", "@e1", "--frame"), True, False),
    (("network", "route", "https://example.com/*"), True, True),
    (("addinitscript", "x"), True, True),
    (("set", "headers", "{}"), True, True),
    (("batch", "click @e1", "eval 1"), True, False),
    (("batch", "click @e1", "network route *"), True, True),
    (("batch", "click 'unclosed"), True, True),
])
def test_scripts_mark_their_document_and_lasting_changes_mark_the_session(args, document, session):
    assert marks.runs_script(args) is document
    assert marks.outlives_document(args) is session


@pytest.mark.asyncio
async def test_a_script_marks_the_document_it_ran_in_and_its_site_for_this_session(on_page):
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("eval", "1"))
    assert marks.scripted(THREAD, "L1") and not marks.scripted(THREAD, "L2")
    assert marks.site_marked(THREAD, "s1", "example.com") and not marks.site_marked(THREAD, "s1", "other.test")
    assert not marks.site_marked(THREAD, "s2", "example.com")
    assert not marks.session_marked(THREAD, "s1")


@pytest.mark.asyncio
async def test_a_new_document_on_a_scripted_site_is_still_marked_by_its_site(on_page):
    # A reload commits a new loader, but a service worker or storage the script left still reaches the site.
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("eval", "1"))
    on_page["page"] = ("L2", "example.com")
    assert not marks.scripted(THREAD, "L2")
    assert marks.site_marked(THREAD, "s1", "example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    ("batch", "open https://example.com/b", "eval 1"),
    ("batch", "tab 2", "eval 1"),
    ("batch", "click @e1", "wait --fn window.ready"),
    ("batch", "--bail", "back", "eval 1"),
])
async def test_a_batch_that_can_run_script_marks_the_whole_session(on_page, args):
    # An earlier step can navigate or switch tabs, so the document a later script runs in isn't known in advance.
    await marks.before_agent_command(THREAD, "s1", "ws://page", args)
    assert marks.session_marked(THREAD, "s1") and not marks.session_marked(THREAD, "s2")


@pytest.mark.asyncio
async def test_a_batch_without_a_script_step_marks_nothing(on_page):
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("batch", "click @e1", "snapshot -i"))
    assert not marks.session_marked(THREAD, "s1") and not marks.scripted(THREAD, "L1")
    assert not marks.site_marked(THREAD, "s1", "example.com")


@pytest.mark.asyncio
async def test_a_page_whose_document_or_address_cant_be_read_marks_the_session(on_page):
    on_page["page"] = None
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("eval", "1"))
    assert marks.session_marked(THREAD, "s1")


@pytest.mark.asyncio
@pytest.mark.parametrize("tree", [
    {},
    {"frameTree": None},
    {"frameTree": {"frame": None}},
    {"frameTree": {"frame": {"loaderId": "L1"}}},
    {"frameTree": {"frame": {"loaderId": "L1", "url": "about:blank"}}},
    {"frameTree": {"frame": {"url": "https://example.com/"}}},
])
async def test_an_unreadable_frame_tree_gives_no_page(monkeypatch, tree):
    class Client:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def call(self, method):
            assert method == "Page.getFrameTree"
            return tree

    monkeypatch.setattr(browser_cdp, "CdpClient", Client)
    assert await marks._page("ws://page") is None


@pytest.mark.asyncio
async def test_a_readable_frame_tree_gives_its_document_and_registrable_domain(monkeypatch):
    class Client:
        def __init__(self, _endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def call(self, _method):
            return {"frameTree": {"frame": {"loaderId": "L9", "url": "https://login.example.com/a"}}}

    monkeypatch.setattr(browser_cdp, "CdpClient", Client)
    assert await marks._page("ws://page") == ("L9", "example.com")


@pytest.mark.asyncio
async def test_a_route_marks_its_session_until_the_browser_closes(on_page):
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("network", "route", "*"))
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("eval", "1"))
    assert marks.session_marked(THREAD, "s1") and not marks.session_marked(THREAD, "s2")
    assert not marks.session_marked("other-thread", "s1")
    marks.forget(THREAD)
    assert not marks.session_marked(THREAD, "s1") and not marks.scripted(THREAD, "L1")
    assert not marks.site_marked(THREAD, "s1", "example.com")


def test_an_unread_document_counts_as_scripted():
    assert marks.scripted(THREAD, "")


def test_a_fill_without_a_session_id_counts_as_marked():
    assert marks.session_marked(THREAD, None)
