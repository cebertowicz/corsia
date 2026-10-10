"""Canonical identity for one live WebSocket connection."""

from __future__ import annotations

from fastapi import WebSocket


def session_id(ws: WebSocket) -> str:
    """Return the process-local presence and delivery identity for one socket."""
    return f"ws:{id(ws)}"
