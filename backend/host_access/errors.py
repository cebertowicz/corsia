"""Leaf exceptions shared by host-access supervision helpers."""

from __future__ import annotations


class HostAccessSupervisorError(RuntimeError):
    """The reviewed lease cannot safely authorize host execution."""
