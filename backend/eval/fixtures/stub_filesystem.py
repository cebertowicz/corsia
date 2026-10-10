
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator


@contextlib.asynccontextmanager
async def stub_filesystem(
    *,
    files: dict[str, str] | None = None,
    git_init: bool = False,
) -> AsyncIterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="corsia-eval-fs-"))
    try:
        for rel, content in (files or {}).items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        if git_init:
            import subprocess
            subprocess.run(
                ["git", "init", "-q"], cwd=str(root), check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "eval@local"],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Eval Harness"],
                check=True, capture_output=True,
            )

        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
