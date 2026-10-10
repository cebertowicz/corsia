"""Exact recorded objects retain deleted/changed versions without current fallback."""
from __future__ import annotations

import hashlib
import subprocess
import threading

from core.guidance_manifest import git_blob_oid
from core.lesson_files import render_trial, with_review_ref
from core.lesson_report_files import recorded_source
from core.lesson_trials import Trial
from infra.guidance_git import recorded_blob_reader
from tests.test_lesson_report_inventory import SUBJECT


def test_recorded_object_survives_a_changed_or_deleted_current_file(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    text = with_review_ref(render_trial(
        lesson_id=SUBJECT, owner="owner", name="deciding", description="Use before deciding.",
        text="The original rule.", trial=Trial("hypothesis", "2026-10-01"),
        source_refs=[{"kind": "test", "reference": "immutable-object-fixture"}],
    ), "review")
    data = text.encode()
    subprocess.run(["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
                   input=data, check=True, capture_output=True)
    path = tmp_path / "SKILL.md"
    path.write_text("A changed and invalid current file.")
    receipt = {"path": str(path), "git_blob_oid": git_blob_oid(data),
               "raw_sha256": hashlib.sha256(data).hexdigest()}
    reader, reason = recorded_blob_reader(tmp_path)
    assert reason is None
    previous, reason = recorded_source(reader, receipt)
    assert reason is None and previous["id"] == SUBJECT
    path.unlink()
    same, reason = recorded_source(reader, receipt)
    assert reason is None and same == previous
    assert recorded_source(reader, receipt | {"raw_sha256": "0" * 64}) == (None, "recorded file identity mismatch")
    assert recorded_source(reader, receipt | {"git_blob_oid": "0" * 40})[0] is None
    assert recorded_source(reader, receipt | {"git_blob_oid": "HEAD:SKILL.md"}) == (
        None, "exact recorded file object unavailable",
    )


def test_a_recorded_non_lesson_does_not_acquire_an_identity_from_its_path(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    data = b"ordinary guidance"
    subprocess.run(["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"],
                   input=data, check=True, capture_output=True)
    reader, reason = recorded_blob_reader(tmp_path)
    assert reason is None
    source, reason = recorded_source(reader, {"path": "/skills/retained/SKILL.md",
        "git_blob_oid": git_blob_oid(data), "raw_sha256": hashlib.sha256(data).hexdigest()})
    assert source is None and reason == "recorded file is not a validated lesson or capture"


def _store(repo, data: bytes) -> str:
    done = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                          input=data, check=True, capture_output=True)
    return done.stdout.decode().strip()


def _recorded_fixture(repo):
    """Reads covering every reader outcome: valid, repeated, missing, wrong type, oversize, bad bytes."""
    from infra.guidance_git import MAX_GUIDANCE_BYTES
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    lessons = []
    for index in range(6):
        lessons.append(with_review_ref(render_trial(
            lesson_id=SUBJECT, owner="owner", name=f"deciding-{index}", description="Use before deciding.",
            text=f"Rule {index}.", trial=Trial("hypothesis", "2026-10-01"),
            source_refs=[{"kind": "test", "reference": f"batch-{index}"}],
        ), "review").encode())
    reads = []

    def add(oid, raw_sha):
        reads.append({"version": None, "raw_sha256": raw_sha, "path": f"/skills/s{len(reads)}/SKILL.md",
                      "git_blob_oid": oid})

    for data in lessons:
        add(_store(repo, data), hashlib.sha256(data).hexdigest())
    first = lessons[0]
    add(git_blob_oid(first), hashlib.sha256(first).hexdigest())       # repeated read
    add(git_blob_oid(first), "0" * 64)                                # identity mismatch
    add("1" * 40, "0" * 64)                                           # missing object
    add("HEAD:SKILL.md", "0" * 64)                                    # not an object id
    add(None, "0" * 64)
    bad = b"\xff\xfe not utf-8"
    add(_store(repo, bad), hashlib.sha256(bad).hexdigest())           # unreadable
    big = b"x" * (MAX_GUIDANCE_BYTES + 1)
    add(_store(repo, big), hashlib.sha256(big).hexdigest())           # over the guidance limit
    empty_tree = subprocess.run(["git", "-C", str(repo), "mktree"], input=b"", check=True,
                                capture_output=True).stdout.decode().strip()
    add(empty_tree, "0" * 64)                                         # exists, not a blob
    return reads


# sha256 of the identified reads of ``_recorded_fixture`` as origin/dev's per-file reader rendered them.
FROZEN_DEV_READS = "fe5210eeb2fcba19ad678605ee070c1563a9ccde3bcc05139b166194715de78e"


def test_batched_reader_renders_the_reads_origin_dev_rendered_with_one_git_process(tmp_path):
    import json
    from core.lesson_report_reads import identify_recorded_reads

    reads = _recorded_fixture(tmp_path)
    reader, reason = recorded_blob_reader(tmp_path)
    assert reason is None
    try:
        batched = identify_recorded_reads(reads, reader)
        # ``info`` rejects the over-limit blob and tree without consuming their bodies, so every
        # record (including malformed and missing ones) stays on one render-scoped process.
        assert reader.spawned == 1
        assert identify_recorded_reads(reads, reader) == batched and reader.spawned == 1
    finally:
        reader.close()

    digest = hashlib.sha256(json.dumps(batched, sort_keys=True, default=str).encode()).hexdigest()
    assert digest == FROZEN_DEV_READS
    reasons = {record["identity_reason"] for record in batched}
    assert {"recorded file identity mismatch", "exact recorded file object unreadable",
            "exact recorded file object unavailable",
            "exact recorded file object unavailable or exceeds guidance limit"} <= reasons


def test_batched_reader_recovers_after_its_git_process_dies(tmp_path):
    data = b"survives a restart"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    oid, sha = _store(tmp_path, data), hashlib.sha256(data).hexdigest()
    reader, _reason = recorded_blob_reader(tmp_path)
    try:
        assert reader(oid, sha) == (data, None)
        reader._proc.kill()
        reader._proc.wait()
        assert reader(oid, sha) == (None, "exact recorded file object unavailable or exceeds guidance limit")
        assert reader(oid, sha) == (data, None) and reader.spawned == 2
    finally:
        reader.close()


def test_a_watchdog_killed_git_process_reports_unavailable_and_recovers(tmp_path):
    from infra.guidance_blob import BatchBlobReader
    data = b"after the watchdog"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    oid, sha = _store(tmp_path, data), hashlib.sha256(data).hexdigest()
    healthy, _reason = recorded_blob_reader(tmp_path)
    reader = BatchBlobReader(["sleep", "30"], healthy._env, 0.2)
    try:
        assert reader(oid, sha) == (None, "exact recorded file object unavailable or exceeds guidance limit")
        assert reader._proc is None
        reader.close()
        reader.close()
        reader._command = healthy._command
        assert reader(oid, sha) == (data, None) and reader.spawned == 2
        reader._proc.kill()
        reader._proc.wait()
        reader.close()
        reader.close()
        assert reader._proc is None
    finally:
        reader.close()
        healthy.close()


def test_a_batch_process_missing_a_pipe_is_a_fault_not_a_crash():
    """Both pipes are required, so their absence faults through the ordinary unavailable path."""
    from infra.guidance_blob import BatchBlobReader

    class NoPipes:
        stdin = stdout = None

        def __init__(self):
            self.killed = self.waited = False

        def kill(self):
            self.killed = True

        def wait(self):
            self.waited = True

    proc = NoPipes()
    reader = BatchBlobReader(["true"], {}, 0.2)
    reader._proc = proc
    assert reader(git_blob_oid(b"never written"), "0" * 64) == (
        None, "exact recorded file object unavailable or exceeds guidance limit")
    assert proc.killed and proc.waited  # close() skipped the absent pipes and still reaped
    assert reader._proc is None and reader.spawned == 0


def test_a_watchdog_that_fires_after_the_response_is_read_restarts_the_next_read(tmp_path, monkeypatch):
    """A kill too late to affect this read still invalidates the process it killed."""
    from infra.guidance_blob import BatchBlobReader
    data = b"too late to matter, too soon to cache"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    oid, sha = _store(tmp_path, data), hashlib.sha256(data).hexdigest()
    fired = []

    class LateTimer(threading.Timer):
        """The callback runs at the read's own cancel() point: the response is already consumed."""

        def start(self):
            pass  # no clock; cancel() below is the firing

        def cancel(self):
            fired.append(self.args[0])
            self.function(*self.args)

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(threading, "Timer", LateTimer)
    reader = BatchBlobReader.for_repo(tmp_path, (), 30)
    try:
        assert reader(oid, sha) == (data, None)  # the read that got killed still returns its bytes
        killed_proc, = fired
        assert killed_proc.poll() is not None  # reaped, not left cached looking alive
        assert reader._proc is None and reader.spawned == 1
        assert reader(oid, sha) == (data, None) and reader.spawned == 2  # a fresh process serves it
    finally:
        reader.close()


def test_batched_reader_keeps_nonblob_and_oversize_objects_on_the_same_process(tmp_path):
    from infra.guidance_git import MAX_GUIDANCE_BYTES
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    good = b"valid after the skipped objects"
    tree = subprocess.run(["git", "-C", str(tmp_path), "mktree"], input=b"100644 blob " +
                          _store(tmp_path, good).encode() + b"\tf\n", check=True,
                          capture_output=True).stdout.decode().strip()
    big = b"x" * (MAX_GUIDANCE_BYTES + 1)
    receipts = [(tree, "0" * 64), (_store(tmp_path, big), hashlib.sha256(big).hexdigest()),
                (git_blob_oid(good), hashlib.sha256(good).hexdigest())]
    expected = [(None, "exact recorded file object unavailable"),
                (None, "exact recorded file object unavailable or exceeds guidance limit"), (good, None)]
    reader, _reason = recorded_blob_reader(tmp_path)
    try:
        consumed = []
        for (oid, sha), result in zip(receipts, expected):
            assert reader(oid, sha) == result
            consumed.append(reader.spawned)
            assert reader._proc is not None  # `info` discarded the object without reading its body
        assert consumed == [1, 1, 1]
        assert reader(*receipts[-1]) == (good, None) and reader.spawned == 1
    finally:
        reader.close()


def test_watchdog_callback_never_outlives_its_read(tmp_path, monkeypatch):
    from infra import guidance_blob
    from infra.guidance_blob import BatchBlobReader
    data = b"read under an in-flight watchdog"
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    oid, sha = _store(tmp_path, data), hashlib.sha256(data).hexdigest()
    healthy, _reason = recorded_blob_reader(tmp_path)
    entered, joining, release, finished = (threading.Event() for _ in range(4))
    timers = []

    class ObservedTimer(threading.Timer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            timers.append(self)

        def join(self, timeout=None):
            joining.set()  # the read is now waiting for the in-flight callback
            super().join(timeout)

    def blocked_kill(proc):
        entered.set()
        release.wait()
        finished.set()

    def releaser():
        entered.wait()
        # The callback stays in flight until the read itself starts waiting on it.
        if joining.wait(timeout=10):
            release.set()
        else:
            release.set()  # unblock the stuck callback so a failing run still ends

    monkeypatch.setattr(guidance_blob.threading, "Timer", ObservedTimer)
    monkeypatch.setattr(BatchBlobReader, "_kill", staticmethod(blocked_kill))
    helper = threading.Thread(target=releaser)
    helper.start()
    reader = BatchBlobReader(healthy._command, healthy._env, 0)
    try:
        assert reader(oid, sha) == (data, None)
        assert joining.is_set() and finished.is_set()
        assert not any(timer.is_alive() for timer in timers)
    finally:
        helper.join()
        reader.close()
        healthy.close()
