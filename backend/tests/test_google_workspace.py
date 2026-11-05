"""google_workspace: refusals, nothing-lost deletes, token handling and account choice. No Google call is made."""

from __future__ import annotations

import json
import shutil
import sys
import types
import uuid
from pathlib import Path

import pytest

import infra.config as config
from connections import account_resolver
from connections.account_errors import AccountError
from tools import google_workspace as gw
from tools import gws_messages, gws_run
from tools.gws_run import Completed, GwsUnavailable

FIXTURES = Path(__file__).parent / "fixtures" / "gws_previews"
SIGN_IN = "ya29." + "a" * 30
ACCOUNT = {"id": "acct-1", "address": "person@example.com", "state": "connected", "provider": "google"}


def recorded(name: str) -> str:
    return (FIXTURES / f"{name}.json").read_text(encoding="utf-8")


class FakeGws:
    """Stands in for the sandboxed program: answers a preview from a recording, real runs from ``real``."""

    def __init__(self, preview: str | None = None, real=None):
        self.preview, self.real = preview, real or {}
        self.calls: list[tuple[list[str], str | None]] = []

    async def __call__(self, words, *, account_id, token, workdir=None):
        self.calls.append((list(words), token))
        if words[-1] == "--dry-run":
            assert token is None, "the preview must carry no sign-in"
            if isinstance(self.preview, Completed):
                return self.preview
            return Completed(0, self.preview, "") if self.preview is not None else Completed(3, "{}", "")
        if isinstance(self.real, (str, Completed)):
            reply = self.real
        else:
            reply = self.real(words) if callable(self.real) else self.real.get(" ".join(words[:3]), "{}")
        return reply if isinstance(reply, Completed) else Completed(0, reply, "")

    def real_runs(self):
        return [words for words, token in self.calls if token is not None]


@pytest.fixture
def workspace():
    folder = Path(config.WORKSPACE) / f"gws-{uuid.uuid4().hex[:8]}"
    folder.mkdir()
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def tool(workspace, monkeypatch):
    async def in_db(function, *args):
        return function(*args)

    async def access_token(account_id):
        return SIGN_IN

    monkeypatch.setattr(gw, "run_in_db", in_db)
    monkeypatch.setattr(gw, "_account", lambda selector, thread_id: ACCOUNT)
    monkeypatch.setattr(gw.client, "access_token", access_token)
    return gw.GoogleWorkspaceTool(workspace=workspace)


def use(monkeypatch, fake: FakeGws) -> FakeGws:
    monkeypatch.setattr(gw, "run_gws", fake)
    return fake


async def test_mail_auth_and_unpreviewable_commands_never_reach_google(tool, monkeypatch):
    fake = use(monkeypatch, FakeGws(recorded("gmail_send_helper")))
    assert await tool.execute(args=["gmail", "+send"]) == gws_messages.MAIL
    assert await tool.execute(args=["auth", "login"]) == gws_messages.NO_PREVIEW
    fake.preview = "{}"  # a preview that succeeds but prints no request
    assert await tool.execute(args=["calendar", "+agenda"]) == gws_messages.NO_PREVIEW
    assert fake.real_runs() == []


async def test_emptying_the_trash_and_unguarded_docs_deletes_are_refused(tool, monkeypatch):
    fake = use(monkeypatch, FakeGws(recorded("drive_empty_trash")))
    assert await tool.execute(args=["drive", "files", "emptyTrash"]) == gws_messages.TRASH
    fake.preview = recorded("docs_delete_unguarded")
    assert await tool.execute(args=["docs", "documents", "batchUpdate"]) == gws_messages.DOCS_GUARD
    assert fake.real_runs() == []


async def test_paths_outside_the_workspace_are_refused(tool, workspace, monkeypatch):
    fake = use(monkeypatch, FakeGws(recorded("drive_list")))
    outside = str(Path(config.WORKSPACE) / "elsewhere.txt")
    for kwargs in ({"upload": outside}, {"output": outside}, {"output": "/etc/hosts"}, {"output": "../x"}):
        assert await tool.execute(args=["drive", "files", "get"], **kwargs) == gws_messages.PATHS
    assert await tool.execute(args=["drive", "files", "get", "--output", "x"]) == gws_messages.PATHS
    assert await tool.execute(args=["drive", "files", "get", "--upload=x"]) == gws_messages.PATHS
    assert fake.calls == []


async def test_workspace_paths_are_passed_to_gws_as_absolute_paths(tool, workspace, monkeypatch):
    fake = use(monkeypatch, FakeGws(recorded("drive_list"), {"drive files get": '{"saved": 1}'}))
    await tool.execute(args=["drive", "files", "get"], output="out/doc.txt", upload="in.txt")
    words = fake.real_runs()[0]
    assert words[words.index("--output") + 1] == str((workspace / "out/doc.txt").resolve())
    assert words[words.index("--upload") + 1] == str((workspace / "in.txt").resolve())


async def test_a_drive_delete_runs_as_a_move_to_the_trash(tool, monkeypatch):
    fake = use(monkeypatch, FakeGws(recorded("drive_delete"), {"drive files update": '{"id": "F1"}'}))
    result = await tool.execute(args=["drive", "files", "delete"], params={"fileId": "F1", "supportsAllDrives": True})
    assert result.startswith(gws_messages.TRASHED)
    assert fake.real_runs() == [[
        "drive", "files", "update", "--params", json.dumps({"fileId": "F1", "supportsAllDrives": True}),
        "--json", json.dumps({"trashed": True}),
    ]]
    assert all("delete" not in words for words, _ in fake.calls if words[-1] != "--dry-run")


async def test_an_event_is_saved_in_full_before_it_is_deleted(tool, workspace, monkeypatch):
    event = json.dumps({"id": "EV1", "summary": "Lunch", "attendees": [{"email": "carol@example.com"}]})
    seen = {}

    def real(words):
        if words[:3] == ["calendar", "events", "get"]:
            assert json.loads(words[4]) == {"calendarId": "primary", "eventId": "EV1"}
            return event
        seen["saved_before_delete"] = sorted((workspace / "google-deletions" / "person@example.com").glob("*"))
        return ""

    use(monkeypatch, FakeGws(recorded("calendar_event_delete"), real))
    result = await tool.execute(args=["calendar", "events", "delete"], params={"calendarId": "primary", "eventId": "EV1"})
    [saved] = seen["saved_before_delete"]
    assert json.loads(saved.read_text(encoding="utf-8")) == [json.loads(event)]
    assert saved.name.endswith("Z-event-EV1.json") and saved.name[8] == "T"
    assert result == gws_messages.SAVED_FIRST.format(where=saved.relative_to(workspace))


async def test_deleting_a_series_also_saves_its_changed_occurrences(tool, workspace, monkeypatch):
    series = {"id": "S1", "iCalUID": "S1@google.com", "recurrence": ["RRULE:FREQ=WEEKLY"]}
    moved = {"id": "S1_20261110T170000Z", "iCalUID": "S1@google.com", "recurringEventId": "S1", "summary": "moved"}
    pages = {None: {"items": [series], "nextPageToken": "p2"}, "p2": {"items": [moved]}}
    lists = []

    def real(words):
        if words[:3] == ["calendar", "events", "get"]:
            return json.dumps(series)
        if words[:3] == ["calendar", "events", "list"]:
            params = json.loads(words[4])
            lists.append(params)
            return json.dumps(pages[params.get("pageToken")])
        return ""

    use(monkeypatch, FakeGws(recorded("calendar_event_delete"), real))
    await tool.execute(args=["calendar", "events", "delete"], params={"calendarId": "primary", "eventId": "S1"})
    assert [params["iCalUID"] for params in lists] == ["S1@google.com", "S1@google.com"]
    [saved] = (workspace / "google-deletions" / "person@example.com").glob("*")
    assert json.loads(saved.read_text(encoding="utf-8")) == [series, moved]


async def test_nothing_is_deleted_when_the_event_cannot_be_saved(tool, workspace, monkeypatch):
    fake = use(monkeypatch, FakeGws(recorded("calendar_event_delete"), Completed(1, "", "not found")))
    result = await tool.execute(args=["calendar", "events", "delete"])
    assert result == gws_messages.NOT_SAVED.format(detail="not found")
    assert [words[:3] for words in fake.real_runs()] == [["calendar", "events", "get"]]
    (workspace / "google-deletions").write_text("a file where the folder should be", encoding="utf-8")
    fake.real = lambda words: '{"id": "EV1"}'
    assert (await tool.execute(args=["calendar", "events", "delete"])).startswith(gws_messages.NOT_SAVED.format(detail="").strip())
    assert [words[:3] for words in fake.real_runs()].count(["calendar", "events", "delete"]) == 0


async def test_clearing_a_calendar_saves_every_page_of_events_first(tool, workspace, monkeypatch):
    pages = {None: {"items": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"}, "p2": {"items": [{"id": "c"}]}}
    order = []

    def real(words):
        if words[:3] == ["calendar", "events", "list"]:
            params = json.loads(words[4])
            order.append(("list", params.get("pageToken")))
            return json.dumps(pages[params.get("pageToken")])
        order.append(("clear", len(list((workspace / "google-deletions" / "person@example.com").glob("*")))))
        return ""

    use(monkeypatch, FakeGws(recorded("calendar_clear"), real))
    result = await tool.execute(args=["calendar", "calendars", "clear"], params={"calendarId": "primary"})
    assert order == [("list", None), ("list", "p2"), ("clear", 1)]
    [saved] = (workspace / "google-deletions" / "person@example.com").glob("*")
    assert saved.name.endswith("Z-calendar-primary.json")
    assert json.loads(saved.read_text(encoding="utf-8")) == [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    assert result == gws_messages.SAVED_FIRST.format(where=saved.relative_to(workspace))


async def test_deleting_a_calendar_saves_its_events_first(tool, workspace, monkeypatch):
    order = []

    def real(words):
        if words[:3] == ["calendar", "events", "list"]:
            order.append("list")
            return json.dumps({"items": [{"id": "a"}]})
        order.append(("delete", len(list((workspace / "google-deletions" / "person@example.com").glob("*")))))
        return ""

    use(monkeypatch, FakeGws(recorded("calendar_delete"), real))
    result = await tool.execute(args=["calendar", "calendars", "delete"], params={"calendarId": "abc@group.calendar.google.com"})
    assert order == ["list", ("delete", 1)]
    [saved] = (workspace / "google-deletions" / "person@example.com").glob("*")
    assert result == gws_messages.SAVED_FIRST.format(where=saved.relative_to(workspace))


async def test_creating_and_editing_log_nothing(tool, workspace, monkeypatch):
    fake = use(monkeypatch, FakeGws(recorded("calendar_insert"), '{"id": "new"}'))
    assert await tool.execute(args=["calendar", "events", "insert"], body={"summary": "t"}) == '{"id": "new"}'
    fake.preview = recorded("calendar_patch")
    await tool.execute(args=["calendar", "events", "patch"])
    assert not (workspace / "google-deletions").exists()


async def test_schema_and_help_run_without_a_sign_in(tool, monkeypatch):
    fake = use(monkeypatch, FakeGws(None, lambda words: '{"httpMethod": "GET"}'))
    assert await tool.execute(args=["schema", "drive.files.list"]) == '{"httpMethod": "GET"}'
    assert await tool.execute(args=["drive", "files", "--help"]) == '{"httpMethod": "GET"}'
    assert fake.calls == [(["schema", "drive.files.list"], None), (["drive", "files", "--help"], None)]


async def test_a_missing_gws_is_said_plainly(tool, monkeypatch):
    async def missing(words, **kwargs):
        raise GwsUnavailable(gws_messages.NOT_FOUND)

    monkeypatch.setattr(gw, "run_gws", missing)
    assert await tool.execute(args=["drive", "files", "list"]) == gws_messages.NOT_FOUND


def fake_launch(command: list[str]):
    def launch(words, account_id, sign_in, workdir):
        env = {"PATH": "/usr/bin:/bin", **({gws_run.TOKEN_ENV: sign_in} if sign_in else {})}
        params = types.SimpleNamespace(command=command[0], args=[*command[1:], *words], env=env, cwd=Path.cwd())
        return params, Path(config.WORKSPACE) / f"{uuid.uuid4().hex}.sb", env
    return launch


def test_the_sign_in_is_scrubbed_from_everything_gws_prints(monkeypatch):
    code = (
        "import os, sys\nvalue = os.environ['" + gws_run.TOKEN_ENV + "']\n"
        "print('echo ' + value, 'and ya29.other-shaped_value')\nprint('bad ' + value, file=sys.stderr)\nsys.exit(1)"
    )
    monkeypatch.setattr(gws_run, "_launch", fake_launch([sys.executable, "-c", code]))
    done = gws_run._run([], "acct-1", SIGN_IN, None)
    assert done.code == 1
    assert SIGN_IN not in done.stdout + done.stderr and "ya29." not in done.stdout + done.stderr
    assert done.stdout.strip() == "echo [token] and [token]" and done.stderr.strip() == "bad [token]"


def test_the_sign_in_reaches_only_the_environment_never_the_command_line(monkeypatch):
    seen = {}

    def launch(words, account_id, sign_in, workdir):
        seen.update(words=words, sign_in=sign_in)
        return fake_launch([sys.executable, "-c", "pass"])(words, account_id, sign_in, workdir)

    monkeypatch.setattr(gws_run, "_launch", launch)
    words = gws_run.build_args(["drive", "files", "list"], {"pageSize": 1}, None, None, None)
    gws_run._run(words, "acct-1", SIGN_IN, None)
    assert seen["sign_in"] == SIGN_IN and all(SIGN_IN not in word for word in seen["words"])


@pytest.fixture
def no_download(monkeypatch):
    from tools import managed_runtime

    def refuse(*_args, **_kwargs):
        pytest.fail("the gws tool must never download")

    monkeypatch.setattr(managed_runtime, "ensure_runtime", refuse)
    monkeypatch.setattr(managed_runtime, "_download", refuse)


def _bundle_with_gws(tmp_path):
    bundle = tmp_path / "CORSIA.app"
    program = bundle / "Contents" / "Resources" / "libexec" / "gws"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\n", encoding="utf-8")
    return bundle, program


def test_the_bundled_gws_wins_over_an_installed_pin(monkeypatch, tmp_path, no_download):
    from infra import platform_paths
    from tools import managed_runtime

    bundle, program = _bundle_with_gws(tmp_path)
    monkeypatch.setattr(platform_paths, "app_bundle_root", lambda *_a, **_k: bundle)
    monkeypatch.setattr(managed_runtime, "installed_runtime_path", lambda kind, **_k: tmp_path / "installed" / "gws")
    assert gws_run.gws_program() == program


def test_an_installed_pin_is_used_when_there_is_no_bundle(monkeypatch, tmp_path, no_download):
    from infra import platform_paths
    from tools import managed_runtime

    installed = tmp_path / "installed" / "gws"
    monkeypatch.setattr(platform_paths, "app_bundle_root", lambda *_a, **_k: None)
    monkeypatch.setattr(managed_runtime, "installed_runtime_path", lambda kind, **_k: installed if kind == "gws" else None)
    assert gws_run.gws_program() == installed


def test_a_missing_gws_is_not_found_and_nothing_is_fetched(monkeypatch, tmp_path, no_download):
    from infra import platform_paths
    from tools import managed_runtime

    monkeypatch.setattr(platform_paths, "app_bundle_root", lambda *_a, **_k: tmp_path / "Empty.app")
    monkeypatch.setattr(managed_runtime, "installed_runtime_path", lambda kind, **_k: None)
    with pytest.raises(GwsUnavailable) as caught:
        gws_run._launch([], "acct-1", None, None)
    assert str(caught.value) == gws_messages.NOT_FOUND == (
        "Google Workspace isn't available right now: CORSIA couldn't find its gws program."
    )


def test_the_installed_lookup_fetches_nothing_and_finds_only_a_verified_pin(monkeypatch, tmp_path, no_download):
    from tools import managed_runtime

    root = tmp_path / "runtimes"
    assert managed_runtime.installed_runtime_path("gws", root=root) is None
    arch = managed_runtime.macos_arch()
    selected = f"linux-{arch}" if managed_runtime.platform.system() == "Linux" else arch
    pin = managed_runtime.RUNTIME_PINS["gws"][selected]
    install = managed_runtime._install_dir("gws", selected, pin, root)
    install.mkdir(parents=True)
    (install / "gws").write_text("#!/bin/sh\n", encoding="utf-8")
    (install / "gws").chmod(0o755)
    assert managed_runtime.installed_runtime_path("gws", root=root) is None
    managed_runtime._marker_path(install).write_text(
        managed_runtime.json.dumps({"kind": "gws", "arch": selected, **managed_runtime.asdict(pin)}), encoding="utf-8",
    )
    assert managed_runtime.installed_runtime_path("gws", root=root) == install / "gws"


@pytest.fixture
def two_accounts(monkeypatch):
    class Db:
        def close(self):
            pass

    rows = [
        {"id": "g1", "address": "work@example.com", "state": "connected", "provider": "google"},
        {"id": "g2", "address": "home@example.com", "state": "connected", "provider": "google"},
    ]
    import infra.database

    monkeypatch.setattr(infra.database, "get_db", lambda: Db())
    monkeypatch.setattr(account_resolver, "list_accounts", lambda provider, db: list(rows))
    monkeypatch.setattr(account_resolver, "visible_accounts", lambda thread, db, provider: list(rows))
    return rows


def test_one_reachable_account_is_used_without_a_name(two_accounts):
    del two_accounts[1]
    assert gw._account(None, "thread-1")["id"] == "g1"


def test_the_account_is_named_by_address_or_id_and_must_be_named_when_there_are_two(two_accounts):
    assert gw._account("HOME@example.com", "thread-1")["id"] == "g2"
    assert gw._account("g1", "thread-1")["id"] == "g1"
    for selector in (None, "", "nobody@example.com"):
        with pytest.raises(AccountError) as caught:
            gw._account(selector, "thread-1")
        assert str(caught.value).startswith("Name the account: one of ")
        assert "work@example.com" in str(caught.value) and "home@example.com" in str(caught.value)


def test_no_connected_google_account_says_to_connect_one(monkeypatch):
    import infra.database

    monkeypatch.setattr(infra.database, "get_db", lambda: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(account_resolver, "list_accounts", lambda provider, db: [])
    with pytest.raises(AccountError, match="Call google_connect"):
        gw._account(None, "thread-1")
