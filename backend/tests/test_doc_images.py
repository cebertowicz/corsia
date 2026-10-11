"""The image markdown a region comment in a document anchors on (DCOL-FR13.3)."""

from __future__ import annotations

import pytest

from doceditor.comment_action_errors import CommentActionError
from doceditor.doc_images import image_url, widen_to_image
from doceditor.quote_anchor import resolve_quote


@pytest.mark.parametrize(
    ("markdown", "url"),
    [
        ("![Login](img/login.png)", "img/login.png"),
        ("![](a.png)", "a.png"),
        ('![Login](img/login.png "The login screen")', "img/login.png"),
        ("![Login](<my shots/a.png>)", "<my shots/a.png>"),
        ("![a \\] b](x.png)", "x.png"),
        ("![Chart](charts/q(3).png)", "charts/q(3).png"),
        ("![Login](img/login.png) trailing", None),
        ("[Login](img/login.png)", None),
        ("Plain words", None),
    ],
)
def test_image_url_reads_exactly_one_image(markdown, url):
    assert image_url(markdown) == url


TEXT = "Intro é 😀\n\n![Login](img/login.png)\n\nAfter ![Cart](cart.png \"t\") text.\n"


@pytest.mark.parametrize("quote", ["img/login.png", "![Login](img/login.png)", "Login", "](img"])
def test_text_inside_an_image_widens_to_its_whole_markdown(quote):
    start, end = resolve_quote(TEXT, quote)

    widened = widen_to_image(TEXT, start, end)

    markdown = "![Login](img/login.png)"
    expected_start, _ = resolve_quote(TEXT, markdown)
    assert widened[:3] == (markdown, expected_start, expected_start + len(markdown))
    assert widened[3].endswith("😀\n\n") and widened[4].startswith("\n\nAfter ![Cart]")


@pytest.mark.parametrize("quote", ["Intro", "After", "![Login](img/login.png)\n\nAfter"])
def test_text_outside_any_single_image_is_refused(quote):
    start, end = resolve_quote(TEXT, quote)

    with pytest.raises(CommentActionError) as raised:
        widen_to_image(TEXT, start, end)

    assert raised.value.kind == "region_target"
