"""Microsoft Graph OneDrive adapter."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from connections.microsoft import client
from connections.microsoft.errors import MicrosoftError, PROVIDER_UNAVAILABLE

DRIVE = "https://graph.microsoft.com/v1.0/me/drive"


def _item(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    return {key: value.get(key) for key in ("id", "name", "size", "file", "folder", "webUrl", "lastModifiedDateTime")}


async def list_items(account_id: str, parent_id: str = "root") -> list[dict[str, object]]:
    """List the root or one item folder through Graph's distinct root path."""
    if not isinstance(parent_id, str) or not parent_id:
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    base = f"{DRIVE}/root" if parent_id == "root" else f"{DRIVE}/items/{quote(parent_id, safe='')}"
    values = await client.graph_pages(f"{base}/children", account_id=account_id)
    return [_item(value) for value in values]


async def read_item(account_id: str, item_id: str) -> dict[str, object]:
    value = await client.graph_json("GET", f"{DRIVE}/items/{quote(item_id, safe='')}", account_id=account_id)
    return _item(value)


async def upload_item(account_id: str, name: str, content: str) -> dict[str, object]:
    if not isinstance(name, str) or not name or "/" in name or not isinstance(content, str):
        raise MicrosoftError(PROVIDER_UNAVAILABLE)
    value = await client.graph_bytes(
        "PUT", f"{DRIVE}/root:/{quote(name, safe='')}:/content", account_id=account_id,
        content=content.encode("utf-8"),
    )
    return _item(value)
