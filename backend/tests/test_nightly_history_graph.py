"""Daily history index updates preserve History and never fail maintenance."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest
from loguru import logger


def git(repo, *args, input=None):
    return subprocess.run(["git", "-C", str(repo), *args], input=input, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_history_graph_preserves_versions_and_truncation(tmp_path, monkeypatch):
    from infra import config
    from infra.document_history import list_versions
    from jobs import nightly

    repo = tmp_path / "workspace"
    repo.mkdir()
    git(repo, "init", "-q")
    commits = []
    for i in range(510):
        commits.append(
            f"commit refs/heads/main\ncommitter A <a@example.com> {1700000000 + i * 900} +0000\n"
            f"data 6\nsave!\nM 100644 inline note.md\ndata 4\n{i:03d}\n"
        )
    git(repo, "fast-import", "--quiet", input="".join(commits).encode())
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    monkeypatch.setattr(config, "WORKSPACE", str(repo))
    monkeypatch.setattr(nightly, "_HISTORY_GRAPH_FILTERS", 200)
    before = json.dumps(list_versions(repo, "note.md")).encode()
    assert json.loads(before)[1] is True
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        for _ in range(3):
            await nightly._update_history_graph_safely()
            assert (repo / ".git/objects/info/commit-graphs/commit-graph-chain").exists()
            assert json.dumps(list_versions(repo, "note.md")).encode() == before
    finally:
        logger.remove(sink)
    messages = [r["message"] for r in records]
    assert sum("outcome=still filling commits=510 filters=200 elapsed_ms=" in m for m in messages) == 2
    assert any("outcome=complete commits=510 filters=110 elapsed_ms=" in m for m in messages)


@pytest.mark.asyncio
async def test_history_off_skips_and_git_failure_is_logged(tmp_path, monkeypatch):
    from infra import config
    from jobs.nightly import _update_history_graph_safely

    repo = tmp_path / "workspace"
    repo.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", str(repo))
    await _update_history_graph_safely()
    assert not (repo / ".git").exists()
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "initial")
    graph_dir = repo / ".git/objects/info/commit-graphs"
    graph_dir.mkdir(parents=True)
    (graph_dir / "commit-graph-chain.lock").write_text("")
    records = []
    sink = logger.add(lambda message: records.append(str(message)))
    try:
        await _update_history_graph_safely()
    finally:
        logger.remove(sink)
    assert any("history graph outcome=failed commits=1" in record for record in records)


@pytest.mark.asyncio
async def test_graph_timeout_is_a_warning(tmp_path, monkeypatch):
    from infra import config
    from jobs import nightly

    repo = tmp_path / "workspace"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "initial")
    monkeypatch.setattr(config, "WORKSPACE", str(repo))
    popen = subprocess.Popen

    class TimeoutGraph(popen):
        def communicate(self, *args, **kwargs):
            if "commit-graph" in self.args and kwargs.get("timeout") != 5:
                raise subprocess.TimeoutExpired(self.args, kwargs["timeout"])
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(nightly.subprocess, "Popen", TimeoutGraph)
    records = []
    sink = logger.add(lambda message: records.append(message.record))
    try:
        await nightly._update_history_graph_safely()
    finally:
        logger.remove(sink)
    assert any(r["level"].name == "WARNING" and "outcome=stopped at bound commits=1 filters=unknown elapsed_ms=" in r["message"] for r in records)


@pytest.mark.asyncio
async def test_interrupted_real_graph_write_releases_lock_and_next_run_succeeds(tmp_path, monkeypatch):
    from infra import config
    from jobs import nightly

    repo = tmp_path / "workspace"
    repo.mkdir()
    git(repo, "init", "-q")
    commits = (
        f"commit refs/heads/main\ncommitter A <a@example.com> {1700000000 + i} +0000\n"
        f"data 6\nsave!\nM 100644 inline note.md\ndata 6\n{i:05d}\n"
        for i in range(15000)
    )
    git(repo, "fast-import", "--quiet", input="".join(commits).encode())
    git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    monkeypatch.setattr(config, "WORKSPACE", str(repo))
    lock = repo / ".git/objects/info/commit-graphs/commit-graph-chain.lock"
    popen = subprocess.Popen
    interrupted = []

    class InterruptGraph(popen):
        def communicate(self, *args, **kwargs):
            if "commit-graph" in self.args and not interrupted:
                deadline = time.monotonic() + 20
                while not lock.exists() and self.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.001)
                assert lock.exists() and self.poll() is None, "real Git did not reach an active graph lock"
                os.kill(self.pid, signal.SIGSTOP)
                interrupted.append(True)
                raise subprocess.TimeoutExpired(self.args, kwargs["timeout"])
            return super().communicate(*args, **kwargs)

        def terminate(self):
            super().terminate()
            os.kill(self.pid, signal.SIGCONT)

    monkeypatch.setattr(nightly.subprocess, "Popen", InterruptGraph)
    await nightly._update_history_graph_safely()
    assert interrupted and not lock.exists()
    await nightly._update_history_graph_safely()
    assert (repo / ".git/objects/info/commit-graphs/commit-graph-chain").exists()
    assert not lock.exists()
