"""An image's revision is hashed once per version of its file, not on every comment read (FR13.9, EAS21)."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

from doceditor import image_regions


def test_an_unchanged_image_is_hashed_once_and_a_changed_one_again(tmp_path, monkeypatch):
    hashed = []
    real = image_regions.sha256_file
    monkeypatch.setattr(image_regions, "sha256_file", lambda path: hashed.append(path) or real(path))
    login, cart = tmp_path / "login.png", tmp_path / "cart.png"
    login.write_bytes(b"login v1")
    cart.write_bytes(b"cart v1")

    for _ in range(3):
        image_regions.image_revision(login)
        image_regions.image_revision(cart)
    assert sorted(hashed) == [cart, login]

    login.write_bytes(b"login v2, longer")

    assert image_regions.image_revision(login) == hashlib.sha256(b"login v2, longer").hexdigest()
    assert hashed[2:] == [login]


def test_revisions_read_from_many_threads_past_the_cache_size(tmp_path):
    """Comment lists and deliveries run on the database pool's threads at once."""
    images = []
    for index in range(600):
        image = tmp_path / f"{index}.png"
        image.write_bytes(str(index).encode())
        images.append(image)

    with ThreadPoolExecutor(max_workers=8) as pool:
        revisions = list(pool.map(image_regions.image_revision, images * 2))

    assert revisions == [hashlib.sha256(str(index).encode()).hexdigest() for index in range(600)] * 2
