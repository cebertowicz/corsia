from library.pack_digest import pack_digest


def test_raw_pack_digest_known_answer():
    assert pack_digest({"manifest.yaml": b"hello\n"}) == (
        "5e15c5c917a1d65bab6d67d15990726d35ba0c65182101380cdfcd527cfe7726"
    )


def test_raw_pack_digest_is_independent_of_mapping_order():
    assert pack_digest({"z": b"a", "é": b"b"}) == pack_digest({"é": b"b", "z": b"a"})
