"""The installer-only local setup that runs before CORSIA starts; everything mocked."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

import cli_local_before_start as before_start
import cli_sources
from llm import local_setup, local_setup_download, local_setup_ownership
from llm.local_model import LocalModel
from llm.local_setup_gates import SubscriptionSource
from llm.local_setup_results import DownloadProgress

MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 262_144)
SIZE = local_setup.format_size(MODEL.download_size_bytes)


@pytest.fixture
def entry(monkeypatch, tmp_path):
    """The entry with discovery and log routing stubbed; returns the calls it made."""
    calls: list[str] = []

    async def discover(stages):
        calls.append("discover")
        stages("Checking Claude Code and Codex")

    async def no_limit():
        return None

    monkeypatch.setattr(before_start, "_discover", discover)
    monkeypatch.setattr(before_start, "_quiet_logs", lambda: tmp_path / "install.log")
    monkeypatch.setattr("llm.local_setup_gates.limited_subscription_message", no_limit)
    monkeypatch.setattr("llm.local_setup_gates.local_memory_ok", lambda *_args: True)
    for name in ("STREAM_COMMAND_OUTPUT",):
        monkeypatch.setattr(local_setup_ownership, name, getattr(local_setup_ownership, name))
    for name in ("DOWNLOAD_WAIT_SECONDS", "DOWNLOAD_FOLLOW_SECONDS"):
        monkeypatch.setattr(local_setup, name, getattr(local_setup, name))
    for name in ("STEP", "SECONDS"):
        monkeypatch.setattr(DownloadProgress, name, getattr(DownloadProgress, name))
    return calls


def _subscription(monkeypatch, source):
    async def available():
        return source

    monkeypatch.setattr("llm.local_setup_gates.subscription_available", available)


def _flight(monkeypatch, calls, result, stages=()):
    async def run_flight(*, model=None, into_own_omlx=False, on_stage=None):
        # The installer never names a model: the flight takes oMLX's pick.
        assert model is None and not into_own_omlx
        calls.append("flight")
        for stage in stages:
            on_stage(stage)
        return result

    monkeypatch.setattr(local_setup, "_run_flight", run_flight)


def test_a_working_subscription_needs_no_local_model(monkeypatch, entry, capsys):
    _subscription(monkeypatch, SubscriptionSource("claude-code", "me@example.com"))
    _flight(monkeypatch, entry, {"ok": True})

    assert before_start.run_before_start(force_local=False) == 0
    assert entry == ["discover"]
    assert "Claude Code is signed in as me@example.com; no local model is needed." in capsys.readouterr().out


def test_no_subscription_runs_the_shared_flight_in_installer_mode(monkeypatch, entry, capsys):
    _subscription(monkeypatch, None)
    _flight(monkeypatch, entry, {"ok": True, "model": "m"},
            stages=["Installing", "Installing oMLX", f"Downloading m ({SIZE}, 5%)"])

    assert before_start.run_before_start(force_local=False) == 0
    assert entry == ["discover", "flight"]
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "     Checking Claude Code and Codex",
        "     Installing",
        "     Installing oMLX",
        f"     Downloading m ({SIZE}, 5%)",
        "     Local model is ready.",
    ]
    assert local_setup_ownership.STREAM_COMMAND_OUTPUT is True
    assert DownloadProgress.STEP == 5
    assert DownloadProgress.SECONDS == 10
    # No background hand-off: the wait and the ceiling are the same 4 hours.
    assert local_setup.DOWNLOAD_WAIT_SECONDS == local_setup.DOWNLOAD_FOLLOW_SECONDS == 4 * 60 * 60


def test_local_flag_skips_the_subscription_check(monkeypatch, entry):
    _subscription(monkeypatch, SubscriptionSource("codex", None))
    _flight(monkeypatch, entry, {"ok": True})

    assert before_start.run_before_start(force_local=True) == 0
    assert entry == ["discover", "flight"]


@pytest.mark.parametrize(("result", "why"), [
    ({"ok": False, "message": "Homebrew couldn't install oMLX. Try again."},
     "Homebrew couldn't install oMLX. Try again."),
    ({"ok": True, "downloading": True, "message": "still"},
     "the model download did not finish within 4 hours."),
])
def test_a_failed_flight_exits_non_zero_and_says_why(monkeypatch, entry, capsys, result, why):
    _subscription(monkeypatch, None)
    _flight(monkeypatch, entry, result)

    assert before_start.run_before_start(force_local=False) == before_start.EXIT_FAILED
    out = capsys.readouterr().out
    assert f"The local model couldn't be set up: {why}" in out
    assert "corsia" not in out.replace("CORSIA", "")


NEEDS_A_MODEL = ("     This Mac doesn't have enough memory to run CORSIA's local model, "
                 "and CORSIA needs one working model to start.")
SIGN_IN_LINES = [
    "       Claude Code: run claude and sign in. "
    "Not installed? curl -fsSL https:" "//claude.ai/install.sh | bash",
    "       Codex: run codex and sign in. "
    "Not installed? curl -fsSL https:" "//chatgpt.com/codex/install.sh | bash",
]


def _ineligible(monkeypatch):
    monkeypatch.setattr("llm.local_setup_gates.local_memory_ok", lambda *_args: False)


def test_an_ineligible_mac_without_a_sign_in_says_how_to_get_one(monkeypatch, entry, capsys):
    _subscription(monkeypatch, None)
    _flight(monkeypatch, entry, {"ok": True})
    _ineligible(monkeypatch)

    assert before_start.run_before_start(force_local=False) == before_start.EXIT_NEEDS_PROVIDER
    assert entry == ["discover"]
    assert capsys.readouterr().out.splitlines()[1:] == [
        NEEDS_A_MODEL,
        "     Sign in to Claude Code or Codex, then run the install again:",
        *SIGN_IN_LINES,
    ]


def test_an_ineligible_mac_with_a_limited_plan_says_to_wait_or_sign_in(monkeypatch, entry, capsys):
    async def limited():
        return "Claude Code is signed in but has reached its plan limit."

    _subscription(monkeypatch, None)
    _flight(monkeypatch, entry, {"ok": True})
    _ineligible(monkeypatch)
    monkeypatch.setattr("llm.local_setup_gates.limited_subscription_message", limited)

    assert before_start.run_before_start(force_local=False) == before_start.EXIT_NEEDS_PROVIDER
    assert capsys.readouterr().out.splitlines()[1:] == [
        "     Claude Code is signed in but has reached its plan limit.",
        NEEDS_A_MODEL,
        "     Run the install again once that limit resets, or after you sign in to the other one:",
        *SIGN_IN_LINES,
    ]


def test_an_ineligible_mac_with_the_local_flag_says_to_drop_it(monkeypatch, entry, capsys):
    _flight(monkeypatch, entry, {"ok": True})
    _ineligible(monkeypatch)

    assert before_start.run_before_start(force_local=True) == before_start.EXIT_NEEDS_PROVIDER
    assert entry == ["discover"]
    assert capsys.readouterr().out.splitlines()[1:] == [
        NEEDS_A_MODEL,
        "     Run the install again without --local to use Claude Code or Codex.",
    ]


def test_an_unexpected_error_is_one_line_not_a_traceback(monkeypatch, entry, capsys):
    async def boom(_stages):
        raise RuntimeError("database locked")

    monkeypatch.setattr(before_start, "_discover", boom)

    assert before_start.run_before_start(force_local=False) == before_start.EXIT_FAILED
    out = capsys.readouterr().out
    assert "The local model couldn't be set up: database locked" in out
    assert "Traceback" not in out


def test_heartbeat_notes_a_silent_stage_once(monkeypatch):
    stages = before_start._Stages()
    clock = iter([0.0, 0.0, 11.0, 11.0, 12.0, 12.0])
    monkeypatch.setattr(before_start.time, "monotonic", lambda: next(clock, 12.0))
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda text, **_kw: printed.append(text))
    sleeps = iter(range(4))

    async def sleep(_seconds):
        if next(sleeps, None) is None:
            raise asyncio.CancelledError

    monkeypatch.setattr(before_start.asyncio, "sleep", sleep)
    stages("Starting")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(stages.heartbeat())
    assert printed == ["     Starting", "     Still working: Starting"]


def test_streaming_runs_homebrew_without_capturing_its_output(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: seen.append(kwargs))
    monkeypatch.setattr(local_setup_ownership, "STREAM_COMMAND_OUTPUT", True)
    local_setup_ownership.run_command(["brew", "tap"])
    monkeypatch.setattr(local_setup_ownership, "STREAM_COMMAND_OUTPUT", False)
    local_setup_ownership.run_command(["brew", "tap"])

    assert "capture_output" not in seen[0]
    assert seen[1]["capture_output"] is True
    assert all(kwargs["env"]["HOMEBREW_NO_ASK"] == "1" for kwargs in seen)
    assert not local_setup_ownership.STREAMING.is_set()


def test_installer_download_reports_every_five_percent_and_every_ten_seconds(monkeypatch):
    reports: list[str] = []
    tasks = iter([
        {"task_id": "t", "status": "running", "downloaded_size": 2, "total_size": 100},
        {"task_id": "t", "status": "running", "downloaded_size": 6, "total_size": 100},
        {"task_id": "t", "status": "running", "downloaded_size": 7, "total_size": 100},
        {"task_id": "t", "status": "running", "downloaded_size": 7, "total_size": 100},
        {"task_id": "t", "status": "running", "downloaded_size": 11, "total_size": 100},
        {"task_id": "t", "status": "completed", "downloaded_size": 100, "total_size": 100},
    ])
    now = [0.0]

    class Response:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    async def admin_request(_cfg, method, _path, **_kwargs):
        if method == "POST":
            return Response({"success": True, "task": {"task_id": "t"}})
        now[0] += 6.0
        return Response({"tasks": [next(tasks)]})

    async def pause(_seconds):
        return None

    monkeypatch.setattr("llm.providers.omlx_endpoints.admin_request", admin_request)
    monkeypatch.setattr(local_setup.asyncio, "sleep", pause)
    monkeypatch.setattr(local_setup_download.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(DownloadProgress, "STEP", 5)
    monkeypatch.setattr(DownloadProgress, "SECONDS", 10.0)

    asyncio.run(local_setup._wait_for_download("s", reports.append, MODEL))

    model = MODEL.model
    # Polls land every 6 s: 2% (no step yet), 6% (the 5% step), 7% at 6 s (quiet),
    # 7% at 12 s (the 10-second line), 11% (the 10% step), done.
    assert reports == [
        f"Downloading {model} ({SIZE}, 6%)",
        f"Downloading {model} ({SIZE}, 7%)",
        f"Downloading {model} ({SIZE}, 11%)",
        f"Downloading {model} ({SIZE}, 100%)",
    ]


def test_install_summary_lists_only_what_each_row_knows():
    rows = [
        {"source": "claude-code", "name": "Claude Code", "state": "ready",
         "accounts": [{"label": "me@example.com", "label_verified": True}]},
        {"source": "codex", "name": "Codex", "state": "needs_auth"},
        {"source": "omlx", "name": "Local model", "state": "ready"},
    ]
    assert cli_sources.format_install_summary(rows) == [
        "     Model sources",
        "       Claude Code: signed in as me@example.com",
        "       Codex: installed, not signed in",
        "       Local model: ready",
    ]
    rows = [
        {"source": "claude-code", "name": "Claude Code", "state": "not_installed"},
        {"source": "codex", "name": "Codex", "state": "ready", "limited": True,
         "resets_at": "not-a-time"},
        {"source": "omlx", "name": "Local model", "state": "needs_setup"},
    ]
    lines = cli_sources.format_install_summary(rows)
    assert lines[1:] == [
        "       Claude Code: not installed",
        "       Codex: signed in · limited · back later",
        "       Local model: not set up",
    ]
    assert cli_sources.NONE_READY not in "\n".join(lines)
    assert not any("signed in" in line for line in lines if "Local model" in line)
