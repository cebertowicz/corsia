"""Stage-2b: resolve changed paths → owning PRD slug(s).

PRD-owned mapping (NOT the retired spec registry). Multiple PRDs may own one
path (cross-cutting contracts), so resolution returns the union of matches.
"""

from __future__ import annotations

from pathlib import Path


_MAP = """\
# PRD Path Map

> Generated-from-PRD-frontmatter later; hand-maintained interim.

## PRD PATH MAP (machine-readable)

```
backend/connections/        connections
backend/events/             event-delivery event-model
*/evals.py                  eval
```
"""


# Mirrors the live doc: a prose/example fence appears BEFORE the machine-readable
# heading. Parsing the first fence would read the decoy example, not the real map.
_MAP_WITH_EARLIER_FENCE = """\
# PRD Path Map

> Format — each line is `<path-pattern> <SP> <prd-slug>`:

```
backend/DECOY/              decoy-slug
```

## PRD PATH MAP (machine-readable)

```
backend/connections/        connections
```
"""


def _write_map(tmp_path: Path, content: str) -> Path:
    corsia = tmp_path / "projects" / "corsia" / "shared" / "planning"
    corsia.mkdir(parents=True, exist_ok=True)
    (corsia / "PRD-PATH-MAP.md").write_text(content, encoding="utf-8")
    return tmp_path


class TestPrdsForChangedPaths:
    def test_single_owner(self, tmp_path):
        from reviewer.prd_map import prds_for_changed_paths

        ws = _write_map(tmp_path, _MAP)
        assert prds_for_changed_paths(["backend/connections/gh.py"], ws) == ["connections"]

    def test_multiple_owners_per_path(self, tmp_path):
        from reviewer.prd_map import prds_for_changed_paths

        ws = _write_map(tmp_path, _MAP)
        slugs = prds_for_changed_paths(["backend/events/router.py"], ws)
        assert set(slugs) == {"event-delivery", "event-model"}

    def test_union_across_files_distinct(self, tmp_path):
        from reviewer.prd_map import prds_for_changed_paths

        ws = _write_map(tmp_path, _MAP)
        slugs = prds_for_changed_paths(
            ["backend/connections/gh.py", "backend/api/evals.py"], ws
        )
        assert set(slugs) == {"connections", "eval"}

    def test_no_match_returns_empty(self, tmp_path):
        from reviewer.prd_map import prds_for_changed_paths

        ws = _write_map(tmp_path, _MAP)
        assert prds_for_changed_paths(["frontend/src/App.svelte"], ws) == []

    def test_missing_map_returns_empty(self, tmp_path):
        from reviewer.prd_map import prds_for_changed_paths

        corsia = tmp_path / "projects" / "corsia" / "shared" / "planning"
        corsia.mkdir(parents=True)
        assert prds_for_changed_paths(["backend/connections/gh.py"], tmp_path) == []

    def test_earlier_example_fence_is_skipped(self, tmp_path):
        """Parsing is anchored to the heading — an earlier prose fence is ignored."""
        from reviewer.prd_map import prds_for_changed_paths

        ws = _write_map(tmp_path, _MAP_WITH_EARLIER_FENCE)
        # The decoy fence's pattern must NOT match; only the real block is read.
        assert prds_for_changed_paths(["backend/DECOY/x.py"], ws) == []
        assert prds_for_changed_paths(["backend/connections/gh.py"], ws) == ["connections"]
