from pathlib import Path

import pytest

from tools.host_path_resolution import contains, containment_key, key_contains


@pytest.mark.parametrize(
    ("root", "path", "expected"),
    [
        ("/scope/Beta", "/scope/beta/item", True),
        ("/scope/Café", "/scope/Cafe\u0301/item", True),
        ("/scope/beta/", "/scope/beta", True),
        ("/scope/beta", "/scope/beta2/item", False),
    ],
)
def test_key_and_path_containment_agree(root, path, expected):
    assert key_contains(containment_key(Path(root)), containment_key(Path(path))) is expected
    assert contains(Path(root), Path(path)) is expected
