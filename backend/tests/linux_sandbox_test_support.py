"""Fixtures for tests whose paths must be mountable inside bubblewrap."""
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


@pytest.fixture
def linux_sandbox_root():
    # CI's pytest basetemp is under /dev/shm, which the sandbox deliberately
    # replaces. Use a separate disposable root that can actually be bound.
    with TemporaryDirectory(prefix="corsia-linux-sandbox-", dir="/tmp") as root:
        path = Path(root).resolve()
        assert not path.is_relative_to('/dev') and not path.is_relative_to('/proc')
        yield path
