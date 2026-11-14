"""A browser command that fails unexpectedly leaves a log record, and the record carries no page data."""
from __future__ import annotations

import logging

import pytest

from host_access import browser_control
from host_access.browser_session_state import BrowserSessionError
from tools import browser_runtime
from tools.browser_runtime import COMMAND_FAILED, BrowserCloseTool

SECRET_URL = "https://secret.example.test/results?token=abc"


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)


def _records(caplog, logger_name):
    return [record for record in caplog.records if record.name == logger_name and record.levelno >= logging.WARNING]


async def test_unexpected_command_failure_is_logged_with_traceback_and_no_page_data(monkeypatch, caplog):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("plan write failed")

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", fail)
    with caplog.at_level(logging.WARNING, logger=browser_runtime.__name__):
        result = await browser_runtime._run(["open", SECRET_URL], thread_id="thread-1")

    assert result == f"Error: {COMMAND_FAILED}"
    (record,) = _records(caplog, browser_runtime.__name__)
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    message = record.getMessage()
    assert "thread-1" in message and "open" in message
    assert "secret.example.test" not in message and "token" not in message and SECRET_URL not in message


async def test_close_failure_is_logged_with_traceback(monkeypatch, caplog):
    async def fail(_thread_id):
        raise RuntimeError("close failed")

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "close_browser", fail)
    with caplog.at_level(logging.WARNING, logger=browser_runtime.__name__):
        result = await BrowserCloseTool().execute(_thread_id="thread-2")

    assert result.startswith("Error: browser cleanup could not be verified")
    (record,) = _records(caplog, browser_runtime.__name__)
    assert record.exc_info is not None and "thread-2" in record.getMessage()


async def test_failed_page_probe_is_logged_before_it_is_mapped_to_page_check_failed(caplog):
    async def probe(_known):
        raise RuntimeError("page probe unavailable")

    with caplog.at_level(logging.WARNING, logger=browser_control.__name__):
        with pytest.raises(BrowserSessionError, match=browser_control.PAGE_CHECK_FAILED):
            await browser_control.require_page_access("thread-3", probe, ("snapshot",))

    (record,) = _records(caplog, browser_control.__name__)
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert "thread-3" in record.getMessage()
