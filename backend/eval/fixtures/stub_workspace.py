
from __future__ import annotations

import contextlib
import shutil
import tempfile
from pathlib import Path
from typing import AsyncIterator


_DEFAULT_BOOTSTRAP = {
    "AGENTS.md": "# Agents (eval)\n\nMinimal eval-harness AGENTS.md.\n",
    "USER.md": "# User (eval)\n\nName: Eval User.\n",
    "TOOLS.md": "# Tools (eval)\n\nNo specific tool guidance for eval scenarios.\n",
    "IDENTITY.md": "# Identity (eval)\n\nCORSIA eval harness.\n",
}


@contextlib.asynccontextmanager
async def stub_workspace(
    *,
    bootstrap: dict[str, str] | None = None,
    projects: dict[str, dict[str, str]] | None = None,
    engineering_standards: str | None = None,
) -> AsyncIterator[Path]:
    parent = Path(tempfile.mkdtemp(prefix="corsia-eval-ws-parent-"))
    workspace = parent / "Documents"
    workspace.mkdir(parents=True, exist_ok=True)

    try:
        merged = {**_DEFAULT_BOOTSTRAP, **(bootstrap or {})}
        for filename, content in merged.items():
            (workspace / filename).write_text(content, encoding="utf-8")

        es_content = engineering_standards or "# Engineering Standards (eval)\n"
        (parent / "engineering-standards.md").write_text(es_content, encoding="utf-8")

        for project_id, files in (projects or {}).items():
            pdir = workspace / "projects" / project_id
            pdir.mkdir(parents=True, exist_ok=True)
            for filename, content in files.items():
                (pdir / filename).write_text(content, encoding="utf-8")

        yield workspace
    finally:
        shutil.rmtree(parent, ignore_errors=True)
