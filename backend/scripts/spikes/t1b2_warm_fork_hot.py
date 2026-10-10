"""T1b (part 2) — the same question against a *warm* prefix.

The first T1b run forked a session untouched for days: both variants reported
`cache_read = 0`, which proves nothing about argv, only that no cache entry
existed. Anthropic's prompt cache is short-lived, so the RFC's kill-switch
question — "does dropping the `--system-prompt` swap let the fork read the
session's cache?" — can only be answered against a prefix that is actually hot.

Sequence (all forks; the source session is never mutated):

  1. ii  — warm: inherited prefix, instruction as user prompt
  2. ii  — measure: identical call, should now read the cache it just created
  3. i   — measure: `--system-prompt` swap + `--tools ""` against the same
           warm session; if the swap is what busts the prefix this reads ~0
  4. ii  — re-measure: proves variant (i) did not evict what (ii) established

Evidence script, not shipped code. Usage:
    python backend/scripts/spikes/t1b2_warm_fork_hot.py <transcript.jsonl> <cwd> [model]
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


INSTRUCTION = (
    "Summarize this conversation in exactly one sentence. "
    "Return only that sentence."
)


def _argv(thread_id: str, model: str, *, swap: bool) -> list[str]:
    argv = [
        "claude", "-p",
        "--resume", thread_id,
        "--fork-session",
        "--model", model,
        "--effort", "medium",
    ]
    if swap:
        argv.extend(["--system-prompt", INSTRUCTION])
    argv.extend([
        "--output-format", "json",
        "--safe-mode",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--mcp-config", '{"mcpServers":{}}',
        "--permission-mode", "dontAsk",
    ])
    if swap:
        argv.extend(["--tools", ""])
    argv.extend([
        "--disable-slash-commands",
        "--no-chrome",
        "--max-turns", "1",
    ])
    return argv


def _run(label: str, thread_id: str, model: str, cwd: str, project_dir: Path, *, swap: bool):
    before = set(project_dir.glob("*.jsonl"))
    started = time.monotonic()
    proc = subprocess.run(
        _argv(thread_id, model, swap=swap),
        input=INSTRUCTION.encode(),
        capture_output=True,
        cwd=cwd,
        timeout=1_800,
    )
    elapsed = time.monotonic() - started
    for created in set(project_dir.glob("*.jsonl")) - before:
        created.unlink(missing_ok=True)
    if proc.returncode != 0:
        return {"step": label, "error": proc.stderr.decode()[:400]}
    record = json.loads(proc.stdout.decode())
    usage = record.get("usage") or {}
    read = int(usage.get("cache_read_input_tokens") or 0)
    created_tokens = int(usage.get("cache_creation_input_tokens") or 0)
    plain = int(usage.get("input_tokens") or 0)
    total = read + created_tokens + plain
    return {
        "step": label,
        "variant": "i_system_prompt_swap" if swap else "ii_inherited_prefix",
        "cache_read": read,
        "cache_creation": created_tokens,
        "input_tokens": plain,
        "total_input": total,
        "cache_read_pct": round(100 * read / total, 1) if total else None,
        "wall_seconds": round(elapsed, 1),
    }


def main() -> None:
    transcript = Path(sys.argv[1])
    cwd = sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("SPIKE_CC_MODEL", "")
    thread_id = transcript.stem
    project_dir = transcript.parent
    before = hashlib.sha256(transcript.read_bytes()).hexdigest()

    print(json.dumps({
        "session": thread_id,
        "transcript_bytes": transcript.stat().st_size,
        "model": model,
    }))
    steps = [
        ("1_warm_ii", False),
        ("2_measure_ii", False),
        ("3_measure_i", True),
        ("4_remeasure_ii", False),
    ]
    results = []
    for label, swap in steps:
        result = _run(label, thread_id, model, cwd, project_dir, swap=swap)
        results.append(result)
        print(json.dumps(result))

    print(json.dumps({
        "source_transcript_unchanged": (
            before == hashlib.sha256(transcript.read_bytes()).hexdigest()
        )
    }))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
