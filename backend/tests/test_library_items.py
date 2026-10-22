from __future__ import annotations

import hashlib

import pytest

from library.items import LibraryIntegrityError, fetch_bundle, search_items, source_ref


def _item(*, withdrawn: bool = False) -> dict:
    skill = b"---\nname: sample\ndescription: Sample skill\n---\nUse it.\n"
    return {
        "name": "sample",
        "description": "Create sample reports and summaries.",
        "version": "1.0.0",
        "license": "MIT",
        "source": {
            "repository": "example/library",
            "commit": "a" * 40,
            "path": "skills/sample",
        },
        "files": [{"path": "SKILL.md", "sha256": "sha256:" + hashlib.sha256(skill).hexdigest()}],
        "withdrawn": withdrawn,
        "withdrawn_reason": "unsafe" if withdrawn else None,
    }


def test_fetches_each_file_from_raw_pinned_commit_and_checks_digest():
    item = _item()
    urls: list[str] = []
    data = b"---\nname: sample\ndescription: Sample skill\n---\nUse it.\n"

    result = fetch_bundle(item, fetch=lambda url: (urls.append(url), data)[1])

    assert result == {"SKILL.md": data}
    assert urls == [
        "https://raw.githubusercontent.com/example/library/" + "a" * 40 + "/skills/sample/SKILL.md"
    ]
    assert source_ref(item) == "example/library@" + "a" * 40 + ":skills/sample"


def test_digest_mismatch_refuses_the_whole_bundle_before_any_writer_can_run():
    item = _item()

    with pytest.raises(LibraryIntegrityError, match="SKILL.md"):
        fetch_bundle(item, fetch=lambda _url: b"changed")


def test_search_orders_matches_and_excludes_withdrawn_items():
    matching = _item()
    matching["name"] = "sample-reports"
    other = _item(withdrawn=True)
    other["name"] = "sample-withdrawn"

    assert search_items((matching, other), "sample reports") == [matching]
    assert search_items((matching,), "unrelated") == []
