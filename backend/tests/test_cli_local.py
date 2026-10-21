from __future__ import annotations

import argparse
import urllib.error

import pytest


def _args(**values):
    return argparse.Namespace(**{
        "if_no_subscription": False, "automatic": False, "start_only": False, **values,
    })


def _patch_follow(monkeypatch, states, post=None):
    import cli_local

    calls = []
    flights = iter(states)

    def request(method, path, *args):
        calls.append((method, path, *args))
        if method == "POST":
            return post if post is not None else {"ok": True, "in_progress": True}
        return next(flights)

    monkeypatch.setattr(cli_local, "_read_token", lambda: "service-token")
    monkeypatch.setattr(cli_local, "request_json", request)
    monkeypatch.setattr(cli_local.time, "sleep", lambda _seconds: None)
    return calls


def test_local_setup_starts_server_flight_then_follows_distinct_stages_to_ready(monkeypatch, capsys):
    import cli_local

    calls = _patch_follow(monkeypatch, [
        {"state": "starting", "model": "picked-model"},
        {"state": "starting", "model": "picked-model"},
        {"state": "ready"},
    ])

    assert cli_local.cmd_setup(_args(if_no_subscription=True)) == 0

    assert calls[0] == (
        "POST", "/api/providers/local/setup", "service-token", cli_local.SETUP_TIMEOUT_SECONDS,
        {"no_subscription_only": True, "automatic": False},
    )
    assert {call[:2] for call in calls[1:]} == {("GET", "/api/providers/local/setup")}
    assert capsys.readouterr().out == "Local model is ready.\n"


def test_local_setup_says_corsia_is_not_running_and_runs_nothing_on_refused_connection(monkeypatch, capsys):
    import cli_local

    monkeypatch.setattr(cli_local, "_read_token", lambda: "service-token")
    monkeypatch.setattr(
        cli_local, "request_json",
        lambda *_args: (_ for _ in ()).throw(urllib.error.URLError(ConnectionRefusedError("refused"))),
    )
    assert not hasattr(cli_local, "_run_in_process")

    assert cli_local.cmd_setup(_args()) == 2
    assert capsys.readouterr().out == (
        "CORSIA isn't running.\nRun `corsia start` first, then `corsia local setup`.\n"
    )


def test_lost_post_response_reads_the_status_route_once_and_follows_it(monkeypatch, capsys):
    import cli_local

    calls = _patch_follow(monkeypatch, [{"state": "ready"}])
    original = cli_local.request_json

    def lost_post(method, *args):
        if method == "POST":
            raise TimeoutError("response timed out")
        return original(method, *args)

    monkeypatch.setattr(cli_local, "request_json", lost_post)

    assert cli_local.cmd_setup(_args()) == 0
    assert [call[0] for call in calls] == ["GET"]
    assert capsys.readouterr().out == "Local model is ready.\n"


def test_local_setup_does_not_retry_a_reachable_server_error(monkeypatch, capsys):
    import cli_local

    monkeypatch.setattr(cli_local, "_read_token", lambda: "service-token")
    error = urllib.error.HTTPError("http://127.0.0.1", 500, "server error", {}, None)
    monkeypatch.setattr(cli_local, "request_json", lambda *_args: (_ for _ in ()).throw(error))

    assert cli_local.cmd_setup(_args()) == 2
    assert "could not start on the backend" in capsys.readouterr().out


def test_local_setup_directs_existing_omlx_consent_to_the_app(monkeypatch, capsys):
    import io

    import cli_local

    monkeypatch.setattr(cli_local, "_read_token", lambda: "service-token")
    error = urllib.error.HTTPError(
        "http://127.0.0.1", 409, "Conflict", {},
        io.BytesIO(b'{"detail": {"code": "consent_required"}}'),
    )
    monkeypatch.setattr(cli_local, "request_json", lambda *_args: (_ for _ in ()).throw(error))

    assert cli_local.cmd_setup(_args()) == 2
    assert capsys.readouterr().out == (
        "Another copy of oMLX is already running on this computer.\n"
        "Open the CORSIA app and choose whether to stop it or keep it running.\n"
    )


@pytest.mark.parametrize("state", ["failed", "setup_failed", "start_failed"])
def test_local_setup_prints_failed_flight_result_and_returns_two(monkeypatch, capsys, state):
    import cli_local

    _patch_follow(monkeypatch, [{
        "state": state, "cause": "connection", "message": "The download failed.", "ref": "local-test",
        "fix": "Check the connection, then retry in Settings › Providers.",
    }])

    assert cli_local.cmd_setup(_args()) == 2
    assert capsys.readouterr().out == "The download failed.\nCheck your internet connection, then run `corsia local setup` again. Reference: local-test\n"


# The shared fix with the terminal's retry: one next step, and none to retry for memory.
@pytest.mark.parametrize("cause, expected", [
    ("disk_full", "The local model needs 0.1 GB more free space. Free it up, then run `corsia local setup` again."),
    ("connection", "Check your internet connection, then run `corsia local setup` again."),
    ("memory", "Choose a subscription or API provider instead."),
    ("own_omlx_upgrade_required", "Run `brew upgrade jundot/omlx/omlx`, or install the CORSIA app to use its bundled oMLX, then run `corsia local setup` again."),
    ("other", "CORSIA couldn’t set up the local model. Run `corsia local setup` again."),
])
def test_cli_prints_the_shared_fix_with_the_command_as_its_only_retry(monkeypatch, capsys, cause, expected):
    import cli_local
    from llm import local_setup

    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    local_setup._flight.result = {"ok": False, "cause": cause, "message": "The download failed.", "ref": "local-1", "bytes_to_free": 2048}
    status = local_setup.setup_status()
    _patch_follow(monkeypatch, [status])

    assert cli_local.cmd_setup(_args()) == 2
    assert capsys.readouterr().out.splitlines() == ["The download failed.", f"{expected} Reference: local-1"]


def test_local_setup_idle_flight_while_following_means_the_server_restarted(monkeypatch, capsys):
    import cli_local

    _patch_follow(monkeypatch, [
        {"state": "starting"},
        {"state": "stopped"},
    ])

    assert cli_local.cmd_setup(_args()) == 2
    output = capsys.readouterr().out
    assert "Local setup stopped." in output
    assert "Run `corsia local setup` again." in output


def test_local_setup_prints_skipped_without_following(monkeypatch, capsys):
    import cli_local

    calls = _patch_follow(monkeypatch, [], post={"ok": True, "skipped": "ineligible", "message": "too small"})

    assert cli_local.cmd_setup(_args(automatic=True)) == 11
    assert [call[0] for call in calls] == ["POST"]
    assert capsys.readouterr().out == "too small\n"


def test_a_skip_for_a_signed_in_subscription_exits_zero(monkeypatch, capsys):
    import cli_local

    _patch_follow(monkeypatch, [], post={"ok": True, "skipped": "subscription_available", "message": "Codex is signed in"})

    assert cli_local.cmd_setup(_args(automatic=True)) == 0


def test_start_only_returns_a_distinct_status_without_following(monkeypatch, capsys):
    import cli_local

    calls = _patch_follow(monkeypatch, [], post={"ok": True, "in_progress": True})

    assert cli_local.cmd_setup(_args(start_only=True)) == 10
    assert [call[0] for call in calls] == ["POST"]
    assert capsys.readouterr().out == ""


def test_automatic_setup_prints_why_a_limited_subscription_falls_back_to_local(monkeypatch, capsys):
    import cli_local

    _patch_follow(monkeypatch, [{"state": "ready"}], post={
        "ok": True, "in_progress": True,
        "notice": "Codex is signed in but has reached its plan limit; it resets at 2099-01-01T12:00:00+00:00.",
    })

    assert cli_local.cmd_setup(_args(if_no_subscription=True, automatic=True)) == 0
    assert capsys.readouterr().out == (
        "Codex is signed in but has reached its plan limit; it resets at 2099-01-01T12:00:00+00:00.\n"
        "Local model is ready.\n"
    )


def test_local_setup_follow_timeout_reports_unfinished(monkeypatch, capsys):
    import cli_local

    moments = iter([0.0, 0.0, cli_local.FOLLOW_TIMEOUT_SECONDS])
    _patch_follow(monkeypatch, [{"state": "starting"}])
    monkeypatch.setattr(cli_local.time, "monotonic", lambda: next(moments))

    assert cli_local._follow_server_setup("service-token", "corsia local setup") == 1
    assert capsys.readouterr().out == (
        "Local setup is still running.\n"
        "Run `corsia local setup` again to follow its progress.\n"
    )


def test_local_setup_reports_a_failed_status_read_plainly(monkeypatch, capsys):
    import cli_local

    _patch_follow(monkeypatch, [])
    monkeypatch.setattr(
        cli_local, "request_json",
        lambda method, *_args: {"ok": True} if method == "POST" else (_ for _ in ()).throw(OSError("reset")),
    )

    assert cli_local.cmd_setup(_args()) == 2
    assert "could not read the server state" in capsys.readouterr().out


def test_local_setup_still_downloading_exits_twelve_and_says_it_finishes_in_the_background(monkeypatch, capsys):
    import cli_local

    _patch_follow(monkeypatch, [
        {"state": "starting", "model": "picked-model"},
        {"state": "downloading", "percent": 25},
    ])

    assert cli_local.cmd_setup(_args()) == 12
    assert capsys.readouterr().out == (
        "The download keeps going in CORSIA; the app shows its progress.\n"
        "Run `corsia local setup` again to follow it.\n"
    )


def test_local_setup_says_an_existing_omlx_is_unchanged(monkeypatch, capsys):
    import cli_local

    _patch_follow(monkeypatch, [], post={"ok": True, "existing": True,
                                         "message": "A local model is already connected. Its settings are unchanged."})

    assert cli_local.cmd_setup(_args(automatic=True)) == 0
    assert capsys.readouterr().out == "A local model is already connected. Its settings are unchanged.\n"


def test_local_setup_does_not_call_a_skipped_flight_ready(monkeypatch, capsys):
    import cli_local

    _patch_follow(monkeypatch, [], post={"ok": True, "skipped": "x", "message": "No models."})

    assert cli_local.cmd_setup(_args(automatic=True)) == 11
    assert capsys.readouterr().out == "No models.\n"


def test_the_parser_has_no_model_choice_because_omlx_picks_it():
    import pytest

    import cli_local

    parser = argparse.ArgumentParser()
    cli_local.build_parser(parser.add_subparsers(dest="cmd"))
    assert not hasattr(parser.parse_args(["local", "setup"]), "profile")
    with pytest.raises(SystemExit):
        parser.parse_args(["local", "setup", "--profile", "qwen-27b-oq4e"])


def test_a_skipped_setup_with_no_local_model_exits_without_a_provider(monkeypatch, capsys):
    import cli_local
    from llm.local_setup_prepare import NO_LOCAL_MODEL

    _patch_follow(monkeypatch, [], post={"ok": True, "skipped": "ineligible", "message": NO_LOCAL_MODEL})

    assert cli_local.cmd_setup(_args()) == cli_local.EXIT_NO_PROVIDER
    assert capsys.readouterr().out == f"{NO_LOCAL_MODEL}\n"
