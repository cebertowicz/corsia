from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

# benchmarks/ sits at the repository root, outside backend/. The L3 job runs
# pytest from backend/, where the root is not on sys.path.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from benchmarks.harbor.bridge import BridgeError, HarborBridge  # noqa: E402


class Environment:
    """A fake Harbor environment whose filesystem is one host directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    async def exec(self, command, **kwargs):
        return f"ran:{command}:{kwargs['cwd']}:{kwargs['timeout_sec']}"

    async def upload_file(self, source_path, target_path):
        target = self.root / target_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target)

    async def download_file(self, source_path, target_path):
        shutil.copyfile(self.root / source_path.lstrip("/"), target_path)


def _bridge(tmp_path):
    container = tmp_path / "container"
    container.mkdir()
    bridge = HarborBridge(
        Environment(container), tmp_path / "bridge.sock", tmp_path / "trace.jsonl",
        staging_dir=tmp_path / "staging",
    )
    return bridge, container


def _trace(tmp_path):
    return [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]


def test_bridge_executes_only_through_environment(tmp_path):
    bridge, _ = _bridge(tmp_path)

    result = asyncio.run(bridge._dispatch({
        "operation": "exec", "command": "pwd", "cwd": "/work", "timeout": 10,
    }))

    assert result == "ran:pwd:/work:10"
    assert _trace(tmp_path)[0]["command"] == "pwd"


def test_bridge_write_then_read_round_trips_through_container(tmp_path):
    bridge, container = _bridge(tmp_path)

    asyncio.run(bridge._dispatch({"operation": "write", "path": "/app/hello.txt", "content": "hi"}))
    result = asyncio.run(bridge._dispatch({"operation": "read", "path": "/app/hello.txt"}))

    assert result == "hi"
    assert (container / "app/hello.txt").read_text() == "hi"
    assert not [p for p in (tmp_path / "staging").iterdir()]
    assert [row["operation"] for row in _trace(tmp_path)] == ["write", "read"]


def test_bridge_upload_and_download_stay_in_staging(tmp_path):
    bridge, container = _bridge(tmp_path)
    (tmp_path / "staging").mkdir()
    (tmp_path / "staging/in.txt").write_text("payload")

    asyncio.run(bridge._dispatch({"operation": "upload", "path": "/app/in.txt", "local_name": "in.txt"}))
    asyncio.run(bridge._dispatch({"operation": "download", "path": "/app/in.txt", "local_name": "out.txt"}))

    assert (container / "app/in.txt").read_text() == "payload"
    assert (tmp_path / "staging/out.txt").read_text() == "payload"


@pytest.mark.parametrize("request_body", [
    {"operation": "upload", "path": "/app/x", "local_name": "../escape"},
    {"operation": "download", "path": "/app/x", "local_name": "/etc/passwd"},
    {"operation": "read", "path": "relative.txt"},
    {"operation": "write", "path": "/app/x"},
    {"operation": "delete", "path": "/app/x"},
])
def test_bridge_refuses_host_escapes_and_unknown_operations(tmp_path, request_body):
    bridge, _ = _bridge(tmp_path)

    with pytest.raises(BridgeError):
        asyncio.run(bridge._dispatch(request_body))
