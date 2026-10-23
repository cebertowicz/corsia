"""Fact collection owns its readers through timeout and caller cancellation."""

import asyncio

import pytest

from onboarding import first_run_wake


@pytest.mark.asyncio
@pytest.mark.parametrize("caller_cancels", [False, True], ids=["timeout", "caller-cancel"])
async def test_fact_collection_joins_reader_cleanup(monkeypatch, caller_cancels):
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    readers = []

    async def read():
        readers.append(asyncio.current_task())
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()

    monkeypatch.setattr(first_run_wake, "FACTS_TIMEOUT_SECONDS", 60 if caller_cancels else 0.01)
    collection = asyncio.create_task(first_run_wake._read_facts({"setup": read}))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if caller_cancels:
            collection.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        assert not collection.done(), "fact collection returned before its reader finished cleanup"
        release.set()
        if caller_cancels:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(collection, 1)
        else:
            assert await asyncio.wait_for(collection, 1) == {
                "setup": {"unavailable": first_run_wake.UNAVAILABLE_READS["setup"]},
            }
        assert finished.is_set()
    finally:
        release.set()
        collection.cancel()
        for reader in readers:
            if not reader.done() and not cleaning.is_set():
                reader.cancel()
        await asyncio.gather(collection, *readers, return_exceptions=True)
