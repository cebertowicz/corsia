
from __future__ import annotations

import contextlib
import subprocess
from typing import AsyncIterator
from unittest.mock import patch


@contextlib.asynccontextmanager
async def subprocess_log(
    *,
    delegate: bool = True,
) -> AsyncIterator[list[dict]]:
    captured: list[dict] = []
    real_run = subprocess.run

    def fake_run(args, **kwargs):
        record: dict = {"args": args, "kwargs": dict(kwargs)}
        if delegate:
            result = real_run(args, **kwargs)
            record["returncode"] = result.returncode
            record["stdout"] = getattr(result, "stdout", None)
            record["stderr"] = getattr(result, "stderr", None)
            captured.append(record)
            return result

        captured.append(record)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout=b"", stderr=b"")

    with patch("subprocess.run", new=fake_run):
        yield captured
