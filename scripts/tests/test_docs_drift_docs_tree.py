"""End-to-end coverage of citations and relative links in nested docs."""

from check_docs_drift.citation_check import check_citations


def test_docs_tree_citations_and_links(tmp_path):
    (tmp_path / "backend" / "api").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "corsia").write_text("#!/bin/sh\n")
    docs = tmp_path / "docs" / "nested"
    docs.mkdir(parents=True)
    (docs / "target.md").write_text("# Hello, World!\n# Hello, World!\n")
    (docs / "source.md").write_text(
        "[good](target.md#hello-world)\n"
        "[duplicate](target.md#hello-world-1)\n"
        "[missing](absent.md)\n"
        "[bad anchor](target.md#absent)\n"
        "[external](https://example.com/missing)\n"
        "[reference][good-ref]\n[good-ref]: target.md#hello-world-1\n"
        "```md\n[example](not-a-real-file.md)\n```\n"
        "`backend/dead.py`\n"
    )

    result = check_citations(tmp_path)

    assert result.missing_paths == ["docs/nested/source.md: backend/dead.py"]
    assert result.missing_links == [
        "docs/nested/source.md: absent.md: target file missing",
        "docs/nested/source.md: target.md#absent: heading anchor missing",
    ]
    assert not result.ok
