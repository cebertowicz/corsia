"""Hide every saved value from what an authenticated script hands back toward agents.

One matcher for the whole product: ``browser_output_blanking.blank`` over the values its one reader returns
(every saved password and saved item, read by that module, never here). A script's output and error text, its
tool-channel arguments and both forwarders' responses pass through here. When the values can't be read, the text
is withheld rather than passed on unchecked: :class:`Unchecked` says so, and its caller has fixed text for it.
"""

from __future__ import annotations

from typing import Any

from host_access import browser_output_blanking


class Unchecked(Exception):
    """The saved values couldn't be read, so the text couldn't be checked."""


def saved_values() -> frozenset[str]:
    """Every saved value to hide, read once for one run, request or call; :class:`Unchecked` when unreadable."""
    try:
        return browser_output_blanking._saved_values()
    except Exception:  # noqa: BLE001 - any failure withholds the text; the caller's text is fixed
        raise Unchecked from None


def text(value: str, values: frozenset[str]) -> str:
    return browser_output_blanking.blank(value, values) if values else value


def data(value: bytes, values: frozenset[str]) -> bytes:
    """Bytes as UTF-8, any other bytes kept as they are, with every saved value hidden."""
    if not values or not value:
        return value
    return text(value.decode("utf-8", errors="surrogateescape"), values).encode("utf-8", errors="surrogateescape")


def structure(value: Any, values: frozenset[str]) -> Any:
    """A JSON value with every string in it, object keys included, hidden."""
    if isinstance(value, str):
        return text(value, values)
    if isinstance(value, list):
        return [structure(item, values) for item in value]
    if isinstance(value, dict):
        return {text(key, values): structure(item, values) for key, item in value.items()}
    return value
