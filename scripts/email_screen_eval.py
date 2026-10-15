#!/usr/bin/env python3
"""Run the synthetic email set through the local screen once and write a report.

Not part of CI. It measures the one real model the screen would use; it sets no pass bar and
tunes nothing. It boots a throwaway state (a fresh workspace, state directory and database),
registers the local oMLX server you name in it, and sends each message through the same
``screen_message`` path the tools use, so the loopback check and the output validator are the
ones that ship. Nothing here touches a real instance's data, and no real mail is involved.

    python scripts/email_screen_eval.py --server-port 5200 \
        --model-id Qwen3.8-Flash-Next-MLX-oQ6-MTP --report report.md

Run it once per model you want a figure for; each message is classified exactly once, with no
retries, so a failure is reported as a failure.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET = ROOT / "backend" / "tests" / "fixtures" / "email_screen" / "v1.jsonl"
SERVER_ID = "ns-email-screen-eval"


def _hardware() -> str:
    def sysctl(name: str) -> str:
        try:
            return subprocess.run(
                ["sysctl", "-n", name], capture_output=True, text=True, timeout=5, check=False,
            ).stdout.strip()
        except OSError:
            return ""

    chip = sysctl("machdep.cpu.brand_string") or platform.processor() or platform.machine()
    mem = sysctl("hw.memsize")
    gib = f"{int(mem) / 2**30:.0f} GiB" if mem.isdigit() else "unknown memory"
    return f"{chip}, {gib}, {platform.system()} {platform.release()}"


def _boot(server_port: int, model_id: str) -> str:
    """Create a throwaway database that knows one loopback oMLX server and one model; return its id."""
    state = tempfile.mkdtemp(prefix="email-screen-eval-state-")
    os.environ.update(
        CORSIA_WORKSPACE=tempfile.mkdtemp(prefix="email-screen-eval-ws-"),
        CORSIA_STATE_DIR=state,
        CORSIA_TEST_MODE="1",
        CORSIA_ANALYTICS_DISABLE="1",
    )
    sys.path.insert(0, str(ROOT / "backend"))
    from infra.database import get_db, init_db
    from infra.settings import set_setting

    init_db()
    db = get_db()
    now = "2026-01-01T00:00:00+00:00"
    from infra.time import utc_now_iso

    fresh = utc_now_iso()
    db.execute(
        "INSERT INTO network_servers (id, provider, kind, instance_id, instance_name, port, status,"
        " last_seen_at, created_at, updated_at) VALUES (?, 'omlx', 'managed', ?, 'eval', ?, 'online', ?, ?, ?)",
        (SERVER_ID, SERVER_ID, server_port, fresh, now, now),
    )
    db.execute(
        "INSERT INTO network_server_endpoints (id, server_id, host, kind, last_ok_at, last_latency_ms,"
        " created_at, updated_at) VALUES ('ep-eval', ?, '127.0.0.1', 'local', ?, 1, ?, ?)",
        (SERVER_ID, fresh, now, now),
    )
    transport = f"omlx@{SERVER_ID}/{model_id}"
    db.execute(
        "INSERT INTO models (id, harness, provider, provider_model_id, litellm_model, display_name, backend,"
        " source, enabled, available, metadata_json, created_at, updated_at)"
        " VALUES ('eval-model', 'native', 'omlx', ?, ?, ?, 'network', 'discovered', 1, 1, '{}', ?, ?)",
        (model_id, transport, model_id, now, now),
    )
    db.commit()
    set_setting("agents", {"defaults": {"corrections_checker_model": "eval-model"}})
    return transport


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


async def _run(rows: list[dict]) -> list[dict]:
    from connections.google import email_screen
    from infra.database import get_db
    from infra.time import utc_now_iso

    results = []
    for row in rows:
        # The server is registered by hand and no health sweep runs, so keep its endpoint live.
        db = get_db()
        db.execute("UPDATE network_server_endpoints SET last_ok_at = ? WHERE id = 'ep-eval'", (utc_now_iso(),))
        db.commit()
        started = time.monotonic()
        screen = await email_screen.screen_message("eval@example.com", row["id"], row["headers"], row["body"])
        results.append({
            "id": row["id"], "expected_injection": row["expected_injection"],
            "expected_rule": row["expected_rule"], "status": screen.status,
            "injection": screen.injection, "rule": screen.rule, "kind": screen.kind,
            "needs_person": screen.needs_person, "seconds": round(time.monotonic() - started, 1),
        })
    return results


def _report(rows: list[dict], results: list[dict], *, model: str, hardware: str, digest: str, set_path: Path) -> str:
    by_id = {r["id"]: r for r in rows}
    ok = [r for r in results if r["status"] == "screened"]
    missing = [r for r in results if r["status"] != "screened"]
    attacks = [r for r in results if r["expected_injection"]]
    ordinary = [r for r in results if not r["expected_injection"]]
    misses = [r for r in attacks if r["status"] == "screened" and not r["injection"]]
    false_flags = [r for r in ordinary if r["status"] == "screened" and r["injection"]]
    wrong_rule = [r for r in attacks if r["status"] == "screened" and r["injection"] and r["rule"] != r["expected_rule"]]

    def block(r: dict) -> str:
        m = by_id[r["id"]]
        text = m["body"].replace("\n", " ")
        return (
            f"- `{r['id']}` expected {m['expected_rule']}, got "
            f"{r['rule'] if r['status'] == 'screened' else r['status']}\n"
            f"  - subject: {m['headers']['subject']}\n  - body: {text}"
        )

    def rate(n: int, d: int) -> str:
        return f"{n}/{d} ({100 * n / d:.1f}%)" if d else "n/a"

    lines = [
        "# Email screen evaluation",
        "",
        f"- Model: `{model}`",
        f"- Hardware: {hardware}",
        f"- Set: `{set_path.name}`, {len(rows)} synthetic messages, sha256 `{digest}`",
        f"- Run: {time.strftime('%Y-%m-%d %H:%M %Z')}; each message screened once, no retries, no tuning",
        "",
        "## Results",
        "",
        f"- Screened: {rate(len(ok), len(results))}; unavailable or failed: {len(missing)}",
        f"- Attacks missed (flag false): {rate(len(misses), len(attacks))}",
        f"- Attacks caught with a different rule than expected: {rate(len(wrong_rule), len(attacks))}",
        f"- False flags on ordinary mail: {rate(len(false_flags), len(ordinary))}",
        f"- Median seconds per message: {sorted(r['seconds'] for r in results)[len(results) // 2] if results else 'n/a'}",
        "",
        "## Misses",
        "",
        *(block(r) for r in misses),
        *(["(none)"] if not misses else []),
        "",
        "## False flags",
        "",
        *(block(r) for r in false_flags),
        *(["(none)"] if not false_flags else []),
        "",
        "## Caught under another rule",
        "",
        *(block(r) for r in wrong_rule),
        *(["(none)"] if not wrong_rule else []),
        "",
        "## Not screened",
        "",
        *(block(r) for r in missing),
        *(["(none)"] if not missing else []),
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server-port", type=int, required=True, help="port of the local oMLX server on 127.0.0.1")
    parser.add_argument("--model-id", required=True, help="the model id the server lists")
    parser.add_argument("--set", type=Path, default=DEFAULT_SET)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    rows = _load(args.set)
    digest = hashlib.sha256(args.set.read_bytes()).hexdigest()
    transport = _boot(args.server_port, args.model_id)
    results = asyncio.run(_run(rows))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        _report(rows, results, model=transport.partition("/")[2], hardware=_hardware(), digest=digest, set_path=args.set)
    )
    print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
