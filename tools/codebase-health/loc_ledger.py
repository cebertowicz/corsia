"""cloc-backed LOC ledger metrics."""

from __future__ import annotations

import os
import shutil
import subprocess


def loc_ledger(wt: str) -> dict:
    if not shutil.which("cloc"):
        return {}

    os.chdir(wt)
    be_total = _cloc("backend", "tools", "--include-lang=Python")
    be_test = _cloc(
        "backend",
        "tools",
        "--include-lang=Python",
        r"--match-f=(test_|_test\.py$|conftest)",
    )
    be_src = {k: be_total[k] - be_test[k] for k in be_total}
    be_src["files"] = be_total["files"] - be_test["files"]

    fe = _cloc("frontend/src", "--include-lang=Svelte,TypeScript", r"--not-match-f=\.d\.ts$")
    specs = _cloc("specs", "--include-lang=Markdown")
    docs = _cloc(
        ".",
        "--include-lang=Markdown",
        "--exclude-dir=__pycache__,.venv,node_modules,.svelte-kit,"
        "dist,coverage,.worktrees,specs",
    )

    return {
        "loc_be_src_files": be_src["files"],
        "loc_be_src_code": be_src["code"],
        "loc_be_src_comment": be_src["comment"],
        "loc_be_src_blank": be_src["blank"],
        "loc_be_test_files": be_test["files"],
        "loc_be_test_code": be_test["code"],
        "loc_be_test_comment": be_test["comment"],
        "loc_be_test_blank": be_test["blank"],
        "loc_fe_files": fe["files"],
        "loc_fe_code": fe["code"],
        "loc_fe_comment": fe["comment"],
        "loc_fe_blank": fe["blank"],
        "loc_specs_files": specs["files"],
        "loc_specs_code": specs["code"],
        "loc_specs_comment": specs["comment"],
        "loc_specs_blank": specs["blank"],
        "loc_docs_files": docs["files"],
        "loc_docs_code": docs["code"],
        "loc_docs_comment": docs["comment"],
        "loc_docs_blank": docs["blank"],
    }


def _cloc(*args: str) -> dict[str, int]:
    cmd = [
        "cloc",
        "--exclude-dir=__pycache__,.venv,node_modules,.svelte-kit,dist,coverage,.worktrees",
        r"--not-match-f=(\.lock$|package-lock\.json$)",
        "--csv",
        "--quiet",
        *args,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    blank = comment = code = files = 0
    for line in result.stdout.splitlines():
        parts = line.split(",")
        if len(parts) < 5 or parts[0].strip() in ("files", "language", "SUM"):
            continue
        try:
            blank += int(parts[2])
            comment += int(parts[3])
            code += int(parts[4])
            files += 1
        except (ValueError, IndexError):
            pass
    return {"files": files, "blank": blank, "comment": comment, "code": code}
