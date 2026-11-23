"""An agent's script in a saved-sign-in session runs only after a credential-only review; its output is blanked."""
import json

import pytest

from host_access import browser_cdp, browser_launch, browser_script_review as review
from host_access import browser_sessions as sessions
from host_access.browser_session_state import BrowserSessionError
from tests.browser_process_fixture import synthetic_browser  # noqa: F401

# The real reviewer call and reader, kept before the autouse fixture replaces them for the session-level tests.
REVIEW = review._review
PAGE_SECRETS = review.page_secrets


@pytest.fixture(autouse=True)
def reviewer(monkeypatch):
    """A stand-in reviewer with a scripted verdict per call; it records each script it was asked about."""
    review._CACHE.clear()
    state = {"verdicts": [], "scripts": [], "events": []}

    async def fake_review(thread_id, script):
        state["scripts"].append(script)
        verdict = state["verdicts"].pop(0)
        if isinstance(verdict, BaseException):
            raise verdict
        return verdict

    monkeypatch.setattr(review, "_review", fake_review)
    monkeypatch.setattr(review, "emit_product_event",
                        lambda category, action, **kwargs: state["events"].append((category, action, kwargs)))

    async def no_secrets(_endpoint):
        return set()

    monkeypatch.setattr(review, "page_secrets", no_secrets)
    yield state
    review._CACHE.clear()


def saved(monkeypatch):
    monkeypatch.setattr(browser_launch, "_owner_key", lambda _: "owner")


async def test_saved_session_eval_is_reviewed_and_allowed_script_runs(synthetic_browser, monkeypatch, reviewer):
    saved(monkeypatch)
    reviewer["verdicts"] = [("allow", "Reads the page title only.")]
    assert await synthetic_browser.owner.run("a", ["eval", "document.title"]) == "ok"
    assert reviewer["scripts"] == ["document.title"]
    assert any(call[-2:] == ("eval", "document.title") for call in synthetic_browser.calls)
    await synthetic_browser.owner.shutdown()


async def test_review_runs_before_the_action_lock_so_the_pane_is_not_held(synthetic_browser, monkeypatch):
    saved(monkeypatch)
    held = []

    async def observe(thread_id, script):
        held.append(synthetic_browser.owner._sessions[thread_id].action_lock.locked())
        return "allow", "Reads the title."

    monkeypatch.setattr(review, "_review", observe)
    assert await synthetic_browser.owner.run("a", ["eval", "document.title"]) == "ok"
    assert held == [False]
    await synthetic_browser.owner.shutdown()


async def test_rejected_script_is_refused_with_the_reason_and_never_runs(synthetic_browser, monkeypatch, reviewer):
    saved(monkeypatch)
    reviewer["verdicts"] = [("reject", "It reads document.cookie.")]
    with pytest.raises(BrowserSessionError) as refused:
        await synthetic_browser.owner.run("a", ["eval", "document.cookie"])
    assert str(refused.value) == ("This script was refused because it could reach a password, card or sign-in "
                                  "token: It reads document.cookie. Use the browser tools to read or act on the page.")
    assert synthetic_browser.calls == []
    await synthetic_browser.owner.shutdown()


async def test_no_verdict_refuses_and_is_not_cached(synthetic_browser, monkeypatch, reviewer):
    saved(monkeypatch)
    reviewer["verdicts"] = [RuntimeError("reviewer unavailable"), ("allow", "Reads the title.")]
    with pytest.raises(BrowserSessionError) as refused:
        await synthetic_browser.owner.run("a", ["eval", "document.title"])
    assert str(refused.value) == review.UNCHECKED
    assert synthetic_browser.calls == []
    assert await synthetic_browser.owner.run("a", ["eval", "document.title"]) == "ok"
    assert reviewer["scripts"] == ["document.title", "document.title"]
    await synthetic_browser.owner.shutdown()


async def test_a_reviewed_script_is_not_reviewed_again(synthetic_browser, monkeypatch, reviewer):
    saved(monkeypatch)
    reviewer["verdicts"] = [("allow", "Reads the title."), ("reject", "Reads a cookie.")]
    assert await synthetic_browser.owner.run("a", ["eval", "document.title"]) == "ok"
    assert await synthetic_browser.owner.run("a", ["eval", "document.title"]) == "ok"
    for _ in range(2):
        with pytest.raises(BrowserSessionError, match="Reads a cookie"):
            await synthetic_browser.owner.run("a", ["eval", "document.cookie"])
    assert reviewer["scripts"] == ["document.title", "document.cookie"]
    assert [event[2]["properties"]["cached"] for event in reviewer["events"]] == [False, True, False, True]
    await synthetic_browser.owner.shutdown()


def test_cache_is_bounded_and_keyed_by_policy_and_exact_text():
    review._CACHE.clear()
    for index in range(review._CACHE_SIZE + 5):
        review._CACHE[review._cache_key(f"script {index}")] = ("allow", "ok")
        if len(review._CACHE) > review._CACHE_SIZE:
            review._CACHE.popitem(last=False)
    assert len(review._CACHE) == review._CACHE_SIZE
    import hashlib
    expected = hashlib.sha256(("policy-v1" + "\0" + "a = 1").encode()).hexdigest()
    assert review._cache_key("a = 1") == expected != review._cache_key("a = 1 ")


async def test_event_carries_no_script_or_reason(synthetic_browser, monkeypatch, reviewer):
    saved(monkeypatch)
    reviewer["verdicts"] = [("reject", "secret-reason-text"), RuntimeError("x")]
    for script in ("document.cookie", "document.title"):
        with pytest.raises(BrowserSessionError):
            await synthetic_browser.owner.run("a", ["eval", script])
    assert [(category, action) for category, action, _ in reviewer["events"]] == [("browser_pane", "script_review")] * 2
    properties = [kwargs["properties"] for _, _, kwargs in reviewer["events"]]
    assert [item["verdict"] for item in properties] == ["reject", "no_verdict"]
    for item in properties:
        assert set(item) == {"verdict", "cached", "latency_ms", "policy_version"}
        assert item["policy_version"] == "policy-v1"
    assert "document" not in json.dumps(reviewer["events"]) and "secret-reason" not in json.dumps(reviewer["events"])
    await synthetic_browser.owner.shutdown()


@pytest.mark.parametrize("args", [["eval", "1+1"], ["wait", "--fn", "true"]])
async def test_sessions_without_a_saved_sign_in_are_not_reviewed(synthetic_browser, monkeypatch, reviewer, args):
    monkeypatch.setattr(browser_launch, "_owner_key", lambda _: None)
    await synthetic_browser.owner.run("a", ["open", "about:blank"])
    assert await synthetic_browser.owner.run("a", args) == "ok"
    assert reviewer["scripts"] == []
    await synthetic_browser.owner.shutdown()


@pytest.mark.parametrize("args", [["eval", "-b", "ZG9jdW1lbnQuY29va2ll"], ["eval", "--stdin"], ["eval", "-b"],
                                  ["eval", "document.title", "extra"], ["eval"]])
async def test_other_eval_forms_stay_refused_without_review(synthetic_browser, monkeypatch, reviewer, args):
    saved(monkeypatch)
    with pytest.raises(BrowserSessionError, match="unavailable in a browser session"):
        await synthetic_browser.owner.run("a", args)
    assert reviewer["scripts"] == [] and synthetic_browser.calls == []
    await synthetic_browser.owner.shutdown()


async def test_pane_eval_is_not_reviewed(synthetic_browser, monkeypatch, reviewer):
    saved(monkeypatch)
    with pytest.raises(BrowserSessionError, match="a browser pane only navigates"):
        await synthetic_browser.owner.run("a", ["eval", "document.title"], pane=True)
    assert reviewer["scripts"] == []
    await synthetic_browser.owner.shutdown()


@pytest.mark.parametrize("args", [["cookies"], ["storage", "local"], ["get", "cdp-url"], ["network", "har", "x"]])
async def test_cookie_commands_still_refused(synthetic_browser, monkeypatch, reviewer, args):
    saved(monkeypatch)
    with pytest.raises(BrowserSessionError, match="not available to agents"):
        await synthetic_browser.owner.run("a", args)
    assert reviewer["scripts"] == []
    await synthetic_browser.owner.shutdown()


async def test_script_configuration_still_refuses_eval(synthetic_browser, monkeypatch, reviewer):
    saved(monkeypatch)
    monkeypatch.setenv("AGENT_BROWSER_INIT_SCRIPTS", "agent.js")
    with pytest.raises(BrowserSessionError, match="unavailable in a browser session"):
        await synthetic_browser.owner.run("a", ["eval", "document.title"])
    assert reviewer["scripts"] == [] and synthetic_browser.calls == []
    await synthetic_browser.owner.shutdown()


def test_review_application_has_the_measured_shape_and_no_page_origin():
    script = "document.querySelector('h1').textContent"
    application = review.application(script)
    assert application == {
        "purpose": "Inspect and operate the page the person is signed into.",
        "user_request": "Do this page step for me.",
        "trigger": {"cadence_seconds": None},
        "script": script,
        "script_path": "scripts/eval.js",
        "script_sha256": __import__("hashlib").sha256(script.encode()).hexdigest(),
        "files": [],
    }
    assert not {"origin", "url", "page", "host", "site"} & set(application)


async def test_review_uses_the_resolved_reviewer_with_the_measured_spec(monkeypatch):
    from host_access import reviewer as reviewer_module, reviewer_target, supervisor
    from host_access import review_models

    target = object()
    seen = {}

    async def resolve(expected=None, *, requester_thread_id=None):
        seen["requester"] = requester_thread_id
        return target

    async def fake_review_application(got_target, application, **kwargs):
        seen.update(target=got_target, application=application, **kwargs)
        return review_models.ScriptApplicationReview(identity=None, verdict="allow", reason="Reads the title.")

    monkeypatch.setattr(reviewer_target, "resolve_reviewer_target", resolve)
    monkeypatch.setattr(supervisor, "_reviewer_binary", lambda got: "binary" if got is target else None)
    monkeypatch.setattr(reviewer_module, "review_application", fake_review_application)
    assert await REVIEW("thread-1", "document.title") == ("allow", "Reads the title.")
    assert seen["requester"] == "thread-1" and seen["target"] is target and seen["binary"] == "binary"
    assert seen["auto_approve"] is True and seen["usage_thread_id"] == "thread-1"
    assert seen["review_spec"].policy == review.BROWSER_SCRIPT_POLICY
    assert seen["review_spec"].model is review.CredentialScriptPayload
    assert seen["application"] == review.application("document.title")


async def test_malformed_verdict_refuses_as_no_verdict(monkeypatch):
    from host_access.failure_reasons import HostAccessReviewValidationError

    review._CACHE.clear()

    async def malformed(_thread_id, _script):
        raise HostAccessReviewValidationError("bad verdict")

    monkeypatch.setattr(review, "_review", malformed)
    monkeypatch.setattr(review, "emit_product_event", lambda *a, **k: True)
    with pytest.raises(BrowserSessionError) as refused:
        await review.check("t", "document.title")
    assert str(refused.value) == review.UNCHECKED and review._CACHE == {}


# ---------------------------------------------------------------- blanking --

COOKIE = "sessiontokenAAAA1111"
FRAME_COOKIE = "frametokenBBBB2222"
PASSWORD = "top-secret-pw"
FRAME_PASSWORD = 'frame"pw\\123'


class FakePage:
    """A page with one password input, a text match, and a cross-origin frame holding its own password input."""

    def __init__(self, _endpoint, **_kwargs):
        self.session_id = None
        self.parents = {}
        self.attached = []
        self.calls = []
        self.trees = {
            None: {"frame": {"url": "https://bank.test/account", "loaderId": "L0"},
                   "childFrames": [{"frame": {"url": "https://bank.test/inner"}}]},
            "frame-1": {"frame": {"url": "https://login.other.test/frame", "loaderId": "L1"}},
        }
        self.values = {(None, 11): PASSWORD, (None, 12): None, ("frame-1", 21): FRAME_PASSWORD}
        self.cookies = {"https://bank.test/account": [{"value": COOKIE}, {"value": "1"}],
                        "https://login.other.test/frame": [{"value": FRAME_COOKIE}]}

    def session_parents(self, _session_id):
        return {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def call(self, method, **params):
        return await self.call_in(None, method, **params)

    async def call_in(self, session, method, **params):
        self.calls.append((session, method, params))
        if method == "Target.setAutoAttach":
            if session is None:
                self.attached.append({"sessionId": "frame-1", "parentSessionId": None,
                                      "targetInfo": {"type": "iframe", "targetId": "F1"}})
            return {}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 7}
        if method == "Page.getFrameTree":
            return {"frameTree": self.trees[session]}
        if method == "DOM.performSearch":
            return {"searchId": f"s-{session}", "resultCount": len([key for key in self.values if key[0] == session])}
        if method == "DOM.getSearchResults":
            return {"nodeIds": [node for owner, node in self.values if owner == session]}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"{session}|{params['nodeId']}"}}
        if method == "Runtime.callFunctionOn":
            owner, node = params["objectId"].split("|")
            return {"result": {"value": self.values[(None if owner == "None" else owner, int(node))]}}
        if method == "Network.getCookies":
            assert session is None
            return {"cookies": [cookie for url in params["urls"] for cookie in self.cookies.get(url, [])]}
        return {}


async def test_page_secrets_reads_cookies_and_password_values_in_every_frame(monkeypatch):
    pages = []
    monkeypatch.setattr(browser_cdp, "CdpClient", lambda endpoint, **kw: pages.append(FakePage(endpoint)) or pages[-1])
    secrets = await PAGE_SECRETS("ws://page")
    assert secrets == {COOKIE, FRAME_COOKIE, PASSWORD, FRAME_PASSWORD}
    cookie_calls = [params for _, method, params in pages[0].calls if method == "Network.getCookies"]
    assert cookie_calls == [{"urls": ["https://bank.test/account", "https://bank.test/inner",
                                      "https://login.other.test/frame"]}]
    released = {session for session, method, _ in pages[0].calls if method == "Runtime.releaseObjectGroup"}
    assert released == {None, "frame-1"}


def test_blank_replaces_values_in_plain_and_json_escaped_text_and_leaves_the_rest():
    text = f'Error: agent-browser command failed: {PASSWORD} and {json.dumps(FRAME_PASSWORD)} and {COOKIE}; title ok'
    blanked = review.blank(text, {PASSWORD, FRAME_PASSWORD, COOKIE})
    assert blanked == 'Error: agent-browser command failed: [hidden] and "[hidden]" and [hidden]; title ok'
    assert review.blank("nothing secret here", {PASSWORD}) == "nothing secret here"


def _saved_eval_result(monkeypatch, synthetic_browser, reviewer, result, secrets):
    saved(monkeypatch)
    reviewer["verdicts"] = [("allow", "Reads the title.")]
    synthetic_browser.result = result

    async def page_values(_endpoint):
        return set(secrets)

    monkeypatch.setattr(review, "page_secrets", page_values)


async def test_saved_session_eval_output_is_blanked(synthetic_browser, monkeypatch, reviewer):
    stdout = json.dumps({"success": True, "data": {"result": {"cookie": f"sid={COOKIE}", "pw": FRAME_PASSWORD,
                                                              "title": "Account"}}})
    _saved_eval_result(monkeypatch, synthetic_browser, reviewer, sessions.CommandResult(0, stdout, ""),
                       {COOKIE, FRAME_PASSWORD})
    rendered = await synthetic_browser.owner.run("a", ["eval", "document.title"])
    assert COOKIE not in rendered and "frame" not in rendered
    assert json.loads(rendered) == {"cookie": "sid=[hidden]", "pw": "[hidden]", "title": "Account"}
    await synthetic_browser.owner.shutdown()


async def test_saved_session_eval_error_output_is_blanked(synthetic_browser, monkeypatch, reviewer):
    stdout = json.dumps({"success": False, "error": f"Uncaught Error: {PASSWORD}"})
    _saved_eval_result(monkeypatch, synthetic_browser, reviewer, sessions.CommandResult(1, stdout, ""), {PASSWORD})
    rendered = await synthetic_browser.owner.run("thread-with-error", ["eval", "document.title"])
    assert rendered == "Error: agent-browser command failed: Uncaught Error: [hidden]"
    await synthetic_browser.owner.shutdown()


async def test_long_output_is_blanked_before_it_is_cut(synthetic_browser, monkeypatch, reviewer):
    # The rendered output is cut at 8,000 characters; a value across the cut must not leave a prefix behind.
    padding = "x" * (8000 - 5)
    stdout = json.dumps({"success": True, "data": {"result": padding + COOKIE}})
    _saved_eval_result(monkeypatch, synthetic_browser, reviewer, sessions.CommandResult(0, stdout, ""), {COOKIE})
    rendered = await synthetic_browser.owner.run("a", ["eval", "document.title"])
    assert rendered.startswith(padding) and COOKIE[:5] not in rendered
    await synthetic_browser.owner.shutdown()


async def test_unreadable_page_values_withhold_the_output(synthetic_browser, monkeypatch, reviewer):
    stdout = json.dumps({"success": True, "data": {"result": COOKIE}})
    _saved_eval_result(monkeypatch, synthetic_browser, reviewer, sessions.CommandResult(0, stdout, ""), set())

    async def unreadable(_endpoint):
        raise BrowserSessionError("This page couldn't be checked")

    monkeypatch.setattr(review, "page_secrets", unreadable)
    assert await synthetic_browser.owner.run("a", ["eval", "document.title"]) == review.WITHHELD
    await synthetic_browser.owner.shutdown()


async def test_unsaved_session_output_is_not_blanked(synthetic_browser, monkeypatch, reviewer):
    monkeypatch.setattr(browser_launch, "_owner_key", lambda _: None)
    called = []

    async def page_values(_endpoint):
        called.append(True)
        return {"ok"}

    monkeypatch.setattr(review, "page_secrets", page_values)
    await synthetic_browser.owner.run("a", ["open", "about:blank"])
    assert await synthetic_browser.owner.run("a", ["eval", "1+1"]) == "ok" and called == []
    await synthetic_browser.owner.shutdown()
