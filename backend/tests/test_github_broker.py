"""The GitHub read broker refuses everything but a read of a tracked repository."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

import pytest

from connections.github_broker import GithubReadBroker, GithubReadRefused, scrub
from events.connectors import github_notifications_feed as feed
from github_broker_fixtures import OK, REPO, REPO_ID, Inner, make_broker, refusal, secret


@pytest.fixture
def inner() -> Inner:
    return Inner()


@pytest.fixture
def broker(inner: Inner) -> GithubReadBroker:
    return make_broker(inner)


@pytest.mark.parametrize("args", [
    ["api", "--method", "GET", "-i", f"repos/{REPO}/pulls/1"],
    ["api", "--method", "HEAD", "-i", f"/repos/{REPO}/pulls/1"],
    ["api", "-i", f"repos/{REPO.upper()}/issues/1/comments?per_page=100&since=2026-09-24T05:00:00Z"],
    ["api", "--method=get", "-i", "notifications?all=true&per_page=50"],
    ["api", "-i", "user"],
    ["api", "-i", "rate_limit"],
])
def test_a_read_of_a_tracked_repository_or_a_listed_path_runs(broker, inner, args) -> None:
    result = broker(args, timeout=5.0)
    assert result.returncode == 0 and result.stdout == OK
    assert inner.calls[0][:2] == ["api", "--method"] and inner.calls[0][2] in ("GET", "HEAD")
    assert inner.calls[0][-1] == args[-1].lstrip("/")


def test_a_custom_media_type_and_conditional_headers_pass(broker, inner) -> None:
    broker(["api", "--method", "GET", "-i", "-H", "Accept: application/vnd.github.diff",
            "-H", "If-None-Match: W/\"abc\"", "-H", "If-Modified-Since: Thu, 24 Sep 2026 05:30:05 GMT",
            f"repos/{REPO}/pulls/7"], timeout=5.0)
    assert inner.calls[0] == [
        "api", "--method", "GET", "-i",
        "-H", "Accept: application/vnd.github.diff", "-H", 'If-None-Match: W/"abc"',
        "-H", "If-Modified-Since: Thu, 24 Sep 2026 05:30:05 GMT", f"repos/{REPO}/pulls/7",
    ]


def test_the_auth_probe_runs_and_nothing_else_of_auth_does(broker, inner) -> None:
    broker(["auth", "status"], timeout=5.0)
    assert inner.calls == [["auth", "status"]]
    inner.calls.clear()
    for args in (["auth", "token"], ["auth", "status", "--show-token"], ["auth", "login"],
                 ["repo", "view"], ["pr", "merge", "1"], []):
        assert refusal(broker, inner, args) == "command_not_allowed"


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE", "post", "OPTIONS"])
def test_a_write_method_is_refused(broker, inner, method) -> None:
    for flag in (["--method", method], [f"--method={method}"], ["-X", method]):
        assert refusal(broker, inner, ["api", *flag, f"repos/{REPO}/issues/1/comments"]) == "method_not_allowed"


@pytest.mark.parametrize("flag", [
    ["--input", "body.json"], ["--paginate"], ["--hostname", "evil.example"], ["--jq", ".x"],
    ["--cache", "1h"], ["-q", ".x"], ["--slurp"], ["--preview", "x"], ["-XGET"],
    ["-f", "title=x"], ["-F", "title=x"], ["--field", "title=x"], ["--raw-field", "title=x"],
])
def test_a_flag_the_broker_has_not_listed_is_refused(broker, inner, flag) -> None:
    assert refusal(broker, inner, ["api", *flag, f"repos/{REPO}/pulls/1"]) == "flag_not_allowed"


@pytest.mark.parametrize("header", [
    "Authorization: token x", "authorization: Bearer x", "Host: evil.example",
    "X-HTTP-Method-Override: DELETE", "Cookie: a=b", "Accept: a\r\nX-Evil: 1", "no-colon",
])
def test_a_header_that_is_not_a_read_header_is_refused(broker, inner, header) -> None:
    assert refusal(broker, inner, ["api", "-H", header, f"repos/{REPO}/pulls/1"]) == "header_not_allowed"


@pytest.mark.parametrize("path", [
    "https://api.github.com/repos/o/r/pulls/1", "http://127.0.0.1:1/x", "//api.github.com/user",
    "https://evil.example/repos/o/r/pulls/1", "repos/o/r@evil.example/x://y",
])
def test_a_url_is_refused_because_the_broker_takes_a_path(broker, inner, path) -> None:
    assert refusal(broker, inner, ["api", path]) == "full_url"


@pytest.mark.parametrize("path", [
    "repos/o/r/../../../user", "repos/o/r/%2e%2e/x", "repos/o/r%2Fx/pulls", "repos/o/r/pulls/1#frag",
    "repos/o/r/pulls\\1", "repos/o/r/ pulls", "repos/o/r//pulls", "repos/o", "repos//r/x", "", "/",
])
def test_a_malformed_or_traversing_path_is_refused(broker, inner, path) -> None:
    assert refusal(broker, inner, ["api", path]) == "path_invalid"


@pytest.mark.parametrize("path", [
    "graphql", "/graphql", "graphql/", "/graphql/", "GraphQL", "GRAPHQL", "graphql?x=1", "/GraphQL/?query=x",
    "%67raphql", "graph%71l", "//graphql",
])
def test_graphql_is_refused_by_name_in_every_spelling(broker, inner, path) -> None:
    for flags in ([], ["--method", "POST"], ["--method", "GET"]):
        assert refusal(broker, inner, ["api", *flags, path]) == "graphql_not_allowed"


def test_a_repository_outside_the_tracked_set_is_refused(broker, inner) -> None:
    assert refusal(broker, inner, ["api", "repos/other/repo/pulls/1"]) == "repo_out_of_scope"
    assert refusal(broker, inner, ["api", "repos/o/r2/pulls/1"]) == "repo_out_of_scope"
    assert refusal(broker, inner, ["api", "repos/O/R/../x"]) == "path_invalid"


@pytest.mark.parametrize("path", [
    "orgs/o/repos", "gists", "user/repos", "notifications/threads/1", "search/code?q=x",
    "app", "enterprises/e", "api/graphql",
])
def test_a_path_outside_the_explicit_list_is_refused(broker, inner, path) -> None:
    assert refusal(broker, inner, ["api", path]) == "path_not_allowed"


def test_a_missing_or_extra_path_is_malformed(broker, inner) -> None:
    assert refusal(broker, inner, ["api", "-i"]) == "malformed_args"
    assert refusal(broker, inner, ["api", "user", "rate_limit"]) == "malformed_args"
    assert refusal(broker, inner, ["api", "--method"]) == "malformed_args"


def test_stdin_input_is_refused(broker, inner) -> None:
    with pytest.raises(GithubReadRefused) as caught:
        broker(["api", "user"], timeout=5.0, input_text="{}")
    assert caught.value.reason == "input_not_allowed" and inner.calls == []


# --- pagination ------------------------------------------------------------

def test_a_pagination_link_to_another_host_is_refused_not_followed(broker, inner) -> None:
    response = feed.GhResponse(200, {"link": '<https://evil.example/repos/o/r/issues?page=2>; rel="next"'}, [])
    path = feed._next_path(response)
    assert refusal(broker, inner, ["api", "-i", path]) == "full_url"


def test_pagination_through_the_broker_stops_at_the_page_cap(broker, inner) -> None:
    # GitHub names the repository by id in a list's next link.
    link = f'<https://api.github.com/repositories/{REPO_ID}/issues/5/comments?per_page=100&page=2>; rel="next"'
    inner.result = (f"HTTP/2.0 200 OK\r\nLink: {link}\r\n\r\n[{{}}]", "", 0)
    items, truncated = feed._get_all(broker, f"repos/{REPO}/issues/5/comments?per_page=100")
    assert truncated and len(items) == feed.MAX_PAGES == len(inner.calls)
    assert inner.calls[1][-1] == f"repositories/{REPO_ID}/issues/5/comments?per_page=100&page=2"
    assert inner.id_reads == [f"repos/{REPO}"], "the id is read once, not once a page"


def test_page_two_of_a_tracked_repository_is_read_by_its_id(broker, inner) -> None:
    broker(["api", "-i", f"repositories/{REPO_ID}/pulls/3/comments?page=2"], timeout=5.0)
    assert inner.calls[0][-1] == f"repositories/{REPO_ID}/pulls/3/comments?page=2"


@pytest.mark.parametrize("path", [
    "repositories/999/issues/5/comments?page=2", f"repositories/{REPO_ID}1/issues/5/comments",
    f"repositories/0{REPO_ID}/issues/5/comments",
])
def test_a_repositories_path_for_an_untracked_id_is_refused(broker, inner, path) -> None:
    assert refusal(broker, inner, ["api", path]) == "repo_out_of_scope"


@pytest.mark.parametrize("path", [
    f"repositories/{REPO_ID}", "repositories", "repositories/abc/issues", f"repositories/{REPO_ID}/../x",
    "repositories//issues", "repositories/%31001/issues",
])
def test_a_malformed_repositories_path_is_refused(broker, inner, path) -> None:
    assert refusal(broker, inner, ["api", path]) == "path_invalid"


def test_an_id_the_broker_cannot_read_leaves_that_repository_out_of_scope(inner) -> None:
    def unreadable(args, *, timeout=30.0):
        if "--jq" in args:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 404")
        return inner(args, timeout=timeout)

    broker = GithubReadBroker(unreadable, tracked=lambda: frozenset({REPO}))
    with pytest.raises(GithubReadRefused) as caught:
        broker(["api", f"repositories/{REPO_ID}/issues/1/comments"], timeout=5.0)
    assert caught.value.reason == "repo_out_of_scope" and inner.calls == []


# --- the rest of the contract ---------------------------------------------

def test_a_refusal_reaches_a_poller_as_a_named_feed_error(broker, inner) -> None:
    with pytest.raises(feed.FeedError) as caught:
        feed.gh_get(broker, "repos/other/repo/pulls/1")
    assert caught.value.error_class == "read_refused" and "repo_out_of_scope" in str(caught.value)
    assert inner.calls == []


def test_the_default_runner_pins_the_host_and_forwards_the_vetted_argv(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0, OK, "")

    monkeypatch.setenv("GH_HOST", "ghe.example")
    monkeypatch.setattr("connections.github_broker.subprocess.run", fake_run)
    GithubReadBroker(tracked=lambda: frozenset({REPO}))(["api", "-X", "get", "-i", f"repos/{REPO}/pulls/1"], timeout=7.0)
    assert seen["argv"] == ["gh", "api", "--method", "GET", "-i", f"repos/{REPO}/pulls/1"]
    assert seen["env"]["GH_HOST"] == "github.com" and seen["timeout"] == 7.0 and seen["check"] is False


def test_the_tracked_set_is_resolved_once_a_minute(monkeypatch) -> None:
    calls: list[int] = []

    def resolve() -> frozenset[str]:
        calls.append(1)
        return frozenset({REPO})

    monkeypatch.setattr("connections.github_broker.tracked_github_repositories", resolve)
    broker = GithubReadBroker(Inner())
    for _ in range(3):
        broker(["api", f"repos/{REPO}/pulls/1"], timeout=5.0)
    assert calls == [1]


def test_every_poller_defaults_to_the_broker(monkeypatch) -> None:
    import asyncio

    from events.connectors import github_notifications as notifications
    from events.connectors import github_pr_quiet_watch as quiet
    from events.connectors import github_repo_activity as activity

    seen: dict[str, Any] = {}

    class Stop(Exception):
        pass

    async def stop_poll(runner, repos):
        seen["activity"] = runner
        raise Stop

    async def stop_cycle(runner, failures=0):
        seen["notifications"] = runner
        return None, 0

    monkeypatch.setattr(notifications, "run_cycle", stop_cycle)
    monkeypatch.setattr(quiet, "disabled_reason", lambda runner: seen.setdefault("quiet", runner) and "test_mode")
    asyncio.run(notifications.notifications_poll_loop())
    asyncio.run(quiet.pr_quiet_watch_loop())
    monkeypatch.setattr(activity.feed, "tracked_github_repositories", lambda: frozenset())
    monkeypatch.setattr(activity, "poll_once", stop_poll)
    with pytest.raises(Stop):
        asyncio.run(activity.repo_activity_poll_loop())
    assert isinstance(seen["notifications"], GithubReadBroker)
    assert seen["notifications"] is seen["quiet"] is seen["activity"]


# --- the token never leaves ------------------------------------------------

def test_scrub_removes_token_shapes_and_known_environment_tokens(monkeypatch) -> None:
    known = "plain-env-secret-value"
    monkeypatch.setenv("GH_TOKEN", known)
    text = " ".join([
        secret("gho"), secret("ghp"), secret("ghs"), "github_pat_" + "A1b2C3d4E5" * 3,
        known, "Authorization: Bearer abc.def", "authorization: token xyz", "keep me",
    ])
    cleaned = scrub(text)
    for leaked in (secret("gho"), secret("ghp"), secret("ghs"), "github_pat_A1b2", known, "abc.def", "xyz"):
        assert leaked not in cleaned
    assert "keep me" in cleaned


def test_a_token_in_output_errors_and_logs_never_survives(broker, inner, caplog) -> None:
    token = secret()
    inner.result = (f"HTTP/2.0 200 OK\r\nX-Echo: {token}\r\n\r\n" + '{"t": "' + token + '"}', f"warn {token}", 0)
    with caplog.at_level(logging.DEBUG):
        result = broker(["api", "-i", "user"], timeout=5.0)
        for args in (["api", f"repos/{token}/x/pulls"], ["api", "-H", f"Authorization: token {token}", "user"],
                     ["api", "--hostname", token, "user"], ["api", f"https://evil.example/{token}"]):
            with pytest.raises(GithubReadRefused) as caught:
                broker(args, timeout=5.0)
            assert token not in str(caught.value) and token not in repr(caught.value.detail)
    assert token not in result.stdout and token not in result.stderr
    assert token not in " ".join(record.getMessage() for record in caplog.records)
    assert token not in " ".join(str(part) for part in result.args)


def test_a_token_shaped_value_inside_a_json_string_is_scrubbed_and_the_document_stays_valid(broker, inner) -> None:
    token = secret()
    comment = f'before {token} after, then curl -H "Authorization: Bearer {token}" -d \'x\' end'
    inner.result = (
        'HTTP/2.0 200 OK\r\nEtag: W/"1"\r\n\r\n' + json.dumps([{"body": comment, "n": 3, "nested": {"k": [comment]}}]),
        "", 0,
    )
    stdout = broker(["api", "-i", f"repos/{REPO}/issues/1/comments"], timeout=5.0).stdout
    assert token not in stdout
    response = feed._parse(stdout)
    assert response is not None and response.headers["etag"] == 'W/"1"'
    [item] = response.body
    assert item["n"] == 3 and item["nested"]["k"] == [item["body"]]
    assert item["body"].startswith("before ") and " after, then curl -H " in item["body"]
    assert item["body"].endswith(" -d 'x' end")
    inner.result = (json.dumps({"body": comment}), "", 0)
    plain = broker(["api", f"repos/{REPO}/issues/1"], timeout=5.0).stdout
    assert token not in plain and " after, then curl -H " in json.loads(plain)["body"]


@pytest.mark.asyncio
async def test_a_quoted_authorization_header_in_a_pr_body_or_comment_does_not_break_the_pollers(inner) -> None:
    from datetime import UTC, datetime

    from events.connectors import github_pr_quiet_watch as watch

    token = secret()
    quoted = 'curl -H "Authorization: Bearer ' + token + '" https://api.github.com/user'
    pull = json.dumps({"state": "open", "body": quoted})
    comment = json.dumps({"id": 1, "body": quoted, "created_at": "2026-10-26T12:00:00Z", "user": {"login": "a"}})

    def serve(args, *, timeout=30.0):
        path = args[-1].split("?")[0]
        body = f"[{comment}]" if path.endswith("/comments") else pull
        return subprocess.CompletedProcess(args, 0, f'HTTP/2.0 200 OK\r\nEtag: W/"9"\r\n\r\n{body}', "")

    broker = GithubReadBroker(serve, tracked=lambda: frozenset({REPO}))
    watch._etags.clear()
    watched = await watch._get(broker, f"repos/{REPO}/pulls/21")
    assert isinstance(watched, dict) and watched["state"] == "open"  # a None body would forget the PR
    assert token not in watched["body"] and watched["body"].startswith("curl -H ")
    thread = {"subject": {"url": f"https://api.github.com/repos/{REPO}/issues/21", "type": "Issue"}}
    [(event_type, payload)] = feed.comment_payloads(
        broker, thread, datetime(2026, 10, 26, tzinfo=UTC), lambda _key: False,
    )
    assert event_type == "issue_comment" and payload["comment"]["id"] == 1
    assert token not in payload["comment"]["body"] and payload["comment"]["body"].startswith("curl -H ")


def test_gh_follows_redirects_but_sends_the_token_only_to_the_configured_host(tmp_path) -> None:
    """Characterises ``gh api`` against two local servers: why the broker need not pre-empt a redirect.

    The configured host redirects to a different host, then to itself. ``gh``
    follows both (so a cross-host redirect cannot be refused before it is
    followed), attaches the credential to the configured host's requests, and
    sends none to the other host. A ``gh`` release that changed this fails here.
    """
    import http.server
    import secrets
    import shutil
    import threading

    gh = shutil.which("gh")
    if gh is None:
        pytest.skip("gh is not installed")
    seen: list[tuple[str, str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            seen.append((self.headers["Host"].split(":")[0], self.path, self.headers.get("Authorization")))
            if self.path == "/api/v3/to-other":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{other.server_port}/other")
            elif self.path == "/api/v3/to-self":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{home.server_port}/api/v3/final")
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    servers = [http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    home, other = servers
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    probe = secrets.token_hex(16)
    env = {**os.environ, "GH_HOST": f"localhost:{home.server_port}", "GH_CONFIG_DIR": str(tmp_path),
           "GH_NO_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1", "GH_ENTERPRISE_" + "TOKEN": probe}
    try:
        for name in ("to-other", "to-self"):
            url = "http" + f"://localhost:{home.server_port}/api/v3/{name}"
            result = subprocess.run([gh, "api", "-i", url], env=env, capture_output=True, text=True, timeout=30)
            assert result.returncode == 0, result.stderr
    finally:
        for server in servers:
            server.shutdown()
    by_host = {(host, path): auth for host, path, auth in seen}
    assert by_host[("127.0.0.1", "/other")] is None, "gh sent its credential to another host"
    assert by_host[("localhost", "/api/v3/to-other")] and by_host[("localhost", "/api/v3/final")]
    assert all(auth is None or probe in auth for auth in by_host.values())


@pytest.mark.asyncio
async def test_the_quiet_watch_conditional_read_passes_the_broker(inner) -> None:
    from events.connectors import github_pr_quiet_watch as watch

    watch._etags.clear()
    broker = make_broker(inner)
    inner.result = ('HTTP/2.0 200 OK\r\nEtag: W/"1"\r\n\r\n{"state": "open"}', "", 0)
    first = await watch._get(broker, f"repos/{REPO}/pulls/13")
    inner.result = ("HTTP/2.0 304 Not Modified\r\n\r\n", "", 0)
    assert await watch._get(broker, f"repos/{REPO}/pulls/13") == first == {"state": "open"}
    assert inner.calls[1][-3:-1] == ["-H", 'If-None-Match: W/"1"'] and inner.calls[1][-1] == f"repos/{REPO}/pulls/13"
