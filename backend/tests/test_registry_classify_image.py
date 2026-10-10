"""Workspace image files classify as ``image``; text and code types are unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

from registry.service import classify


@pytest.mark.parametrize(
    "name",
    ["a.png", "a.jpg", "a.jpeg", "a.gif", "a.webp", "a.svg", "A.PNG", "shot.JpEg", "anim.GIF"],
)
def test_image_extensions_classify_as_image(name):
    assert classify(Path("/ws/" + name)) == "image"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("n.md", "document"), ("n.txt", "document"), ("m.py", "code"), ("x.json", "code"), ("noext", "code")],
)
def test_non_image_types_are_unchanged(name, expected):
    assert classify(Path("/ws/" + name)) == expected
