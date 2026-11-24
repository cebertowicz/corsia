"""Which agent commands mark a document or a whole session against a saved-login fill (browser_fill_marks)."""
from __future__ import annotations

import pytest

from host_access import browser_fill_marks as marks

THREAD = "thread-marks"


@pytest.fixture(autouse=True)
def _fresh():
    marks._scripted.clear()
    marks._session_marks.clear()
    yield
    marks._scripted.clear()
    marks._session_marks.clear()


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
async def test_a_script_marks_only_the_document_it_ran_in(monkeypatch):
    async def loader(_endpoint):
        return "L1"

    monkeypatch.setattr(marks, "_loader", loader)
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("eval", "1"))
    assert marks.scripted(THREAD, "L1") and not marks.scripted(THREAD, "L2")
    assert not marks.session_marked(THREAD, "s1")


@pytest.mark.asyncio
async def test_a_document_that_cant_be_read_marks_every_document(monkeypatch):
    async def unreadable(_endpoint):
        return ""

    monkeypatch.setattr(marks, "_loader", unreadable)
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("eval", "1"))
    assert marks.scripted(THREAD, "L2")


@pytest.mark.asyncio
async def test_a_route_marks_its_session_until_the_browser_closes(monkeypatch):
    async def loader(_endpoint):
        return "L1"

    monkeypatch.setattr(marks, "_loader", loader)
    await marks.before_agent_command(THREAD, "s1", "ws://page", ("network", "route", "*"))
    assert marks.session_marked(THREAD, "s1") and not marks.session_marked(THREAD, "s2")
    assert not marks.session_marked("other-thread", "s1")
    marks.forget(THREAD)
    assert not marks.session_marked(THREAD, "s1") and not marks.scripted(THREAD, "L1")


def test_past_the_cap_every_document_counts_as_marked():
    for index in range(marks._MARKS_KEPT + 1):
        marks._mark(THREAD, f"L{index}")
    assert marks.scripted(THREAD, "L0") and marks.scripted(THREAD, "never-seen")


def test_a_fill_without_a_session_id_counts_as_marked():
    assert marks.session_marked(THREAD, None)
