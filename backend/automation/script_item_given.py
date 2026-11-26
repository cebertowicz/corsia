"""Per-run socket that gives a script the saved items its approval lists as "given to the script".

Only for an item the script must hold, such as a key it signs with. It is a separate socket from the owner's
tool channel (``socket_dir/"v"``, mode 0600): a request is one line of JSON, ``{"name": <Keys item>}``, and the
answer is length-prefixed JSON, ``{"value": ...}`` or ``{"error": <fixed text>}``. It answers only the items this
run's approval lists as given, and only after the fills' own check for the owner passes again; a saved login is
never given. Each value given records one row in the fills' history, with ``site`` "script". Never an
environment variable. Logs carry ids, fixed reasons and exception type names only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from automation.script_items import ItemsUnavailable, RunItems, check_use
from infra import saved_items
from infra.database import write_transaction
from infra.key_store_values import KeyStoreError
from infra.saved_login_results import audit

logger = logging.getLogger(__name__)

SOCKET_ENV = "CORSIA_SCRIPT_GIVEN_SOCKET"
#: The fills' history requires a site; a value given to a script went to no site (follow-up #3428).
SITE = "script"
_NOT_GIVEN = "This script's approval doesn't give that saved item to the script."
_GONE = "Saved item is no longer available to this script."
_FAILED = "The saved item couldn't be read."


def _give(run: RunItems, name: object) -> dict[str, str]:
    use = next((use for use in run.given() if use.name == name), None)
    if use is None or use.name is None:
        return {"error": _NOT_GIVEN}
    try:
        check_use(run.owner, use)
        value = saved_items.item_value_for_fill(use.name)
    except (ItemsUnavailable, KeyStoreError):
        return {"error": _GONE}
    with write_transaction(None, scope="script_item_given") as conn:
        audit(conn, item=use.name, site=SITE, thread_id=run.owner, result="filled", script_id=run.script_id)
    return {"value": value}


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, run: RunItems) -> None:
    try:
        request = json.loads(await reader.readuntil(b"\n"))
        name = request.get("name") if isinstance(request, dict) else None
        answer = await asyncio.to_thread(_give, run, name)
    except Exception as exc:  # noqa: BLE001 - fixed text; only the type is logged
        logger.warning("script given-item socket failed for script %s: %s", run.script_id, type(exc).__name__)
        answer = {"error": _FAILED}
    try:
        data = json.dumps(answer).encode()
        writer.write(len(data).to_bytes(8, "big") + data)
        await writer.drain()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


@asynccontextmanager
async def given_items(path: Path, run: RunItems):
    """Answer this run's given items until it exits."""
    active: set[asyncio.Task] = set()

    def connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(_serve(reader, writer, run=run))
        active.add(task)
        task.add_done_callback(active.discard)

    server = await asyncio.start_unix_server(connected, path=str(path))
    try:
        path.chmod(0o600)
        yield
    finally:
        server.close()
        await server.wait_closed()
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        path.unlink(missing_ok=True)
