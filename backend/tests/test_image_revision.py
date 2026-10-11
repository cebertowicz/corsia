"""An image's revision is hashed once per version of its file, not on every comment read (FR13.9, EAS21)."""

from __future__ import annotations

import hashlib

from doceditor import image_regions


def test_an_unchanged_image_is_hashed_once_and_a_changed_one_again(tmp_path, monkeypatch):
    hashed = []
    real = image_regions.sha256_file
    monkeypatch.setattr(image_regions, "sha256_file", lambda path: hashed.append(path) or real(path))
    login, cart = tmp_path / "login.png", tmp_path / "cart.png"
    login.write_bytes(b"login v1")
    cart.write_bytes(b"cart v1")
    rows = [
        {"region_number": 1, "image_src": str(login), "image_rev": real(login)},
        {"region_number": 1, "image_src": str(cart), "image_rev": real(cart)},
    ]

    for _ in range(3):
        image_regions.annotate_stale(rows, None)
    assert sorted(hashed) == [cart, login]

    login.write_bytes(b"login v2, longer")
    image_regions.annotate_stale(rows, None)

    assert hashed[2:] == [login]
    assert [row["region_stale"] for row in rows] == [True, False]
    assert image_regions.image_revision(login) == hashlib.sha256(b"login v2, longer").hexdigest()
