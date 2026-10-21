"""Tests for the file-size ratchet (scripts/file_size_limits.py)."""

from __future__ import annotations

import pytest


def test_ancestor_directory_named_tests_does_not_silence_the_scan(
    tmp_path, monkeypatch, capsys, stub_tracked_paths
):
    """An ancestor OUTSIDE the repo literally named 'tests' must not match.

    The default `ratchet` fixture's tmp_path never collides with
    `_SKIP_PARTS` or "tests" by construction, which is exactly why this
    needs its own root: `rglob` yields absolute paths, and both the skip-set
    check and `_is_test` must match against the path relative to the repo
    root, never the absolute one.
    """
    import file_size_limits as module
    import size_limit_scan

    root = tmp_path / "tests" / "repo"
    root.mkdir(parents=True)
    monkeypatch.setattr(module, "_ROOT", root)
    monkeypatch.setattr(module, "_LIMITS", root / "quality" / "file-size-limits.json")
    stub_tracked_paths(root)

    target = root / "backend" / "api" / "delegation.py"
    target.parent.mkdir(parents=True)
    target.write_text("\n".join(["x = 1"] * 402) + "\n", encoding="utf-8")
    (root / "quality").mkdir()
    (root / "quality" / "file-size-limits.json").write_text(
        '{"_comment": [], "limits": {"backend/api/delegation.py": 402}}',
        encoding="utf-8",
    )

    sizes, _functions, unreadable, parse_errors = size_limit_scan.measure(root)
    assert sizes == {"backend/api/delegation.py": 402}
    assert unreadable == []
    assert parse_errors == []

    rc = module.main([])
    assert rc == 0
    assert "none over their limit" in capsys.readouterr().out


def test_clean_tree_passes(ratchet, capsys):
    """A tree exactly at its recorded limits passes with no output failures."""
    ratchet.write("backend/foo.py", 402)
    ratchet.write_baseline(limits={"backend/foo.py": 402})

    rc = ratchet.run([])

    assert rc == 0
    assert "none over their limit" in capsys.readouterr().out


def test_tracked_file_growth_fails(ratchet, capsys):
    """A tracked file that grew past its recorded limit fails the check."""
    ratchet.write("backend/foo.py", 405)
    ratchet.write_baseline(limits={"backend/foo.py": 402})

    rc = ratchet.run([])

    err = capsys.readouterr().err
    assert rc == 1
    assert "backend/foo.py: 405 lines, 3 more than its limit of 402" in err


def test_new_file_over_standard_fails_with_new_file_message(ratchet, capsys):
    """A new file over standard gets the new-file message, not the tracked one."""
    ratchet.write("backend/brand_new.py", 450)
    ratchet.write_baseline(limits={})

    rc = ratchet.run([])

    err = capsys.readouterr().err
    assert rc == 1
    assert "over the 400-line standard for a new .py file" in err


def test_new_file_at_or_under_standard_passes(ratchet, capsys):
    """A new file that meets the standard needs no entry and does not fail."""
    ratchet.write("backend/brand_new.py", 400)
    ratchet.write_baseline(limits={})

    rc = ratchet.run([])

    assert rc == 0


def test_shrink_is_reported_but_write_required_to_apply(ratchet, capsys):
    """A shrunk tracked file fails the plain check (nudging --write), unlike growth."""
    ratchet.write("backend/foo.py", 397)
    ratchet.write_baseline(limits={"backend/foo.py": 402})

    rc = ratchet.run([])

    out = capsys.readouterr().out
    assert rc == 1
    assert '"backend/foo.py": 397,   (was 402)' in out


def test_deleted_tracked_file_fails_and_is_named(ratchet, capsys):
    """A tracked file that no longer exists fails the check by name."""
    ratchet.write_baseline(limits={"backend/gone.py": 500})

    rc = ratchet.run([])

    out = capsys.readouterr().out
    assert rc == 1
    assert "remove 'backend/gone.py'   (file no longer exists)" in out


def test_indexed_missing_file_is_reported_as_removed_not_unreadable(ratchet, monkeypatch, capsys):
    """A staged deletion stays eligible for baseline cleanup before its index changes."""
    import size_limit_scan

    ratchet.write_baseline(limits={"backend/gone.py": 500})
    monkeypatch.setattr(size_limit_scan, "tracked_paths", lambda _root: ["backend/gone.py"])

    rc = ratchet.run([])

    captured = capsys.readouterr()
    assert rc == 1
    assert "remove 'backend/gone.py'   (file no longer exists)" in captured.out
    assert "could not be measured" not in captured.err


def test_undecodable_file_fails_the_check(ratchet, capsys):
    """A file that is not valid UTF-8 cannot evade the ratchet by being unreadable."""
    ratchet.write_undecodable("backend/binary_looking.py")
    ratchet.write_baseline(limits={})

    rc = ratchet.run([])

    err = capsys.readouterr().err
    assert rc == 1
    assert "backend/binary_looking.py: could not be measured" in err


def test_unreadable_tracked_file_is_not_reported_as_deleted(ratchet, capsys):
    """A tracked file that became unreadable must not be misreported as gone."""
    ratchet.write_undecodable("backend/foo.py")
    ratchet.write_baseline(limits={"backend/foo.py": 500})

    rc = ratchet.run([])

    captured = capsys.readouterr()
    assert rc == 1
    assert "backend/foo.py: could not be measured" in captured.err
    assert "no longer exists" not in captured.out
    assert "no longer exists" not in captured.err


def test_write_refuses_when_a_file_is_unreadable(ratchet, capsys):
    """--write must not compute a baseline while a file's real size is unknown."""
    ratchet.write("backend/foo.py", 450)
    ratchet.write_undecodable("backend/binary_looking.py")
    ratchet.write_baseline(limits={"backend/foo.py": 450})

    rc = ratchet.run(["--write"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "backend/binary_looking.py" in err
    # Untouched -- refusing to write must not silently drop the unreadable
    # file's tracked entry either, if it happened to already be tracked.
    assert ratchet.read_baseline()["limits"] == {"backend/foo.py": 450}


def test_is_test_excluded_from_scanning(ratchet, capsys):
    """A test file over standard is never measured, never fails."""
    ratchet.write("backend/tests/test_something.py", 900)
    ratchet.write_baseline(limits={})

    rc = ratchet.run([])

    assert rc == 0


# --- (A): --write may lower a tracked limit but never raise one without --adopt.


def test_write_lowers_a_shrunk_tracked_limit_without_adopt(ratchet):
    """A genuine shrink is written without needing --adopt."""
    ratchet.write("backend/foo.py", 450)
    ratchet.write_baseline(limits={"backend/foo.py": 460})

    rc = ratchet.run(["--write"])

    assert rc == 0
    assert ratchet.read_baseline()["limits"]["backend/foo.py"] == 450


def test_write_refuses_to_raise_a_grown_tracked_limit(ratchet, capsys):
    """--write must NOT raise a tracked limit for a file that grew."""
    ratchet.write("backend/foo.py", 405)
    ratchet.write_baseline(limits={"backend/foo.py": 402})

    rc = ratchet.run(["--write"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "backend/foo.py: 402 -> 405" in err
    # The limit on disk must be untouched -- this is the finding at the heart
    # of the fix: --write must never launder growth into a higher ceiling.
    assert ratchet.read_baseline()["limits"]["backend/foo.py"] == 402


def test_write_adopt_raises_a_grown_tracked_limit_deliberately(ratchet):
    """--write --adopt is the one path that may raise a tracked limit."""
    ratchet.write("backend/foo.py", 405)
    ratchet.write_baseline(limits={"backend/foo.py": 402})

    rc = ratchet.run(["--write", "--adopt"])

    assert rc == 0
    assert ratchet.read_baseline()["limits"]["backend/foo.py"] == 405


def test_split_shrink_recorded_without_adopt_alongside_a_new_file(ratchet, capsys):
    """A split (shrink original + new module) records the shrink without --adopt.

    This is the ordinary decomposition workflow this tool exists to support:
    splitting an oversized file both shrinks it and creates a new file likely
    over standard. The shrink must never be held hostage by --adopt just
    because a new file is present in the same run.
    """
    ratchet.write("backend/original.py", 700)
    ratchet.write("backend/new_module.py", 450)
    ratchet.write_baseline(limits={"backend/original.py": 800})

    rc = ratchet.run(["--write"])

    err = capsys.readouterr().err
    assert rc == 1  # still refuses the new-file adoption
    assert "backend/new_module.py" in err
    baseline = ratchet.read_baseline()["limits"]
    assert baseline["backend/original.py"] == 700  # shrink recorded anyway
    assert "backend/new_module.py" not in baseline  # new file still needs --adopt


def test_adopt_does_not_sweep_in_unrelated_growth(ratchet, capsys):
    """--write --adopt must refuse the whole unsafe set when adoption and
    unrelated growth are both present -- not silently raise the grown one.
    """
    ratchet.write("backend/new_module.py", 450)
    ratchet.write("backend/unrelated.py", 405)
    ratchet.write_baseline(
        limits={"backend/unrelated.py": 402}
    )

    rc = ratchet.run(["--write", "--adopt"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "backend/new_module.py" in err
    assert "backend/unrelated.py: 402 -> 405" in err
    baseline = ratchet.read_baseline()["limits"]
    assert "backend/new_module.py" not in baseline
    assert baseline["backend/unrelated.py"] == 402  # untouched, not swept in


def test_write_refuses_new_oversized_file_without_adopt(ratchet, capsys):
    """A brand-new over-standard file is refused by --write without --adopt."""
    ratchet.write("backend/brand_new.py", 450)
    ratchet.write_baseline(limits={})

    rc = ratchet.run(["--write"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "backend/brand_new.py: 450" in err
    assert "backend/brand_new.py" not in ratchet.read_baseline()["limits"]


def test_write_adopt_records_new_oversized_file(ratchet):
    """--write --adopt records a brand-new over-standard file."""
    ratchet.write("backend/brand_new.py", 450)
    ratchet.write_baseline(limits={})

    rc = ratchet.run(["--write", "--adopt"])

    assert rc == 0
    assert ratchet.read_baseline()["limits"]["backend/brand_new.py"] == 450


def test_write_drops_a_deleted_tracked_file_without_adopt(ratchet):
    """--write removes a tracked entry for a file that no longer exists."""
    ratchet.write_baseline(limits={"backend/gone.py": 500})

    rc = ratchet.run(["--write"])

    assert rc == 0
    assert "backend/gone.py" not in ratchet.read_baseline()["limits"]


# --- (B): append-only files are measured but never limited or failed.


def test_append_only_file_growth_never_fails(ratchet, capsys):
    """An append-only file may grow arbitrarily and never fails the check."""
    ratchet.write("backend/infra/database_migrations.py", 7000)
    ratchet.write_baseline(
        limits={},
        append_only={"backend/infra/database_migrations.py": "grows by design"},
    )

    rc = ratchet.run([])

    assert rc == 0


def test_write_never_adds_a_numeric_limit_for_an_append_only_file(ratchet):
    """--write must not give an append-only file a plain numeric limit."""
    ratchet.write("backend/infra/database_migrations.py", 7000)
    ratchet.write_baseline(
        limits={},
        append_only={"backend/infra/database_migrations.py": "grows by design"},
    )

    rc = ratchet.run(["--write"])

    assert rc == 0
    baseline = ratchet.read_baseline()
    assert "backend/infra/database_migrations.py" not in baseline["limits"]
    assert (
        baseline["append_only"]["backend/infra/database_migrations.py"]
        == "grows by design"
    )


# --- repo-wide scope -------------------------------------------------------


@pytest.mark.parametrize("rel_path", ["tools/helper.py", "scripts/deploy.py"])
def test_roots_outside_backend_and_frontend_are_governed(ratchet, capsys, rel_path):
    """tools/ and scripts/ are production source; a new oversized file fails."""
    ratchet.write(rel_path, 450)
    ratchet.write_baseline()

    assert ratchet.run([]) == 1
    assert rel_path in capsys.readouterr().err


@pytest.mark.parametrize(
    "rel_path",
    [
        "backend/conftest.py",
        "frontend/src/lib/__tests__/fixture.ts",
        "frontend/e2e/setup.ts",
        "tools/example_test.py",
        "frontend/src/Widget.spec.svelte",
    ],
)
def test_test_only_paths_stay_excluded_everywhere(ratchet, capsys, rel_path):
    """One canonical exclusion rule, applied across the widened scope."""
    ratchet.write(rel_path, 450)
    ratchet.write_baseline()

    assert ratchet.run([]) == 0
    assert "none over their limit" in capsys.readouterr().out


def test_metric_utils_consumes_the_canonical_standards():
    """The health harness must not restate the limits the gate enforces."""
    import importlib.util
    from pathlib import Path

    import size_limit_scan

    path = Path(__file__).parents[2] / "tools" / "codebase-health" / "metric_utils.py"
    spec = importlib.util.spec_from_file_location("metric_utils", path)
    assert spec and spec.loader
    metric_utils = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(metric_utils)

    assert metric_utils.PY_FILE_LIMIT == size_limit_scan.STANDARDS[".py"]
    assert metric_utils.TS_FILE_LIMIT == size_limit_scan.STANDARDS[".ts"]
    assert metric_utils.SVELTE_FILE_LIMIT == size_limit_scan.STANDARDS[".svelte"]
    assert metric_utils.FUNC_LIMIT == size_limit_scan.FUNCTION_STANDARD
    # Previously divergent cases, now shared in both directions.
    assert metric_utils.is_test("backend/conftest.py")
    assert metric_utils.is_test("frontend/e2e/flows/thing.spec.ts")
    assert not metric_utils.is_test("backend/core/turns.py")


def test_git_listing_failure_fails_closed(ratchet, monkeypatch, capsys):
    """An empty scan would pass everything, so a git failure must fail loudly."""
    import size_limit_scan

    def _explode(_root):
        raise RuntimeError("git ls-files failed")

    monkeypatch.setattr(size_limit_scan, "tracked_paths", _explode)
    ratchet.write_baseline()

    assert ratchet.run([]) == 1
    assert "ratchet failed" in capsys.readouterr().err


# --- function dimension ---------------------------------------------------


def test_new_over_standard_function_fails_with_new_entry_message(ratchet, capsys):
    """A brand-new long function gets no legacy grace."""
    ratchet.write_function("backend/core/thing.py", "big", 81)
    ratchet.write_baseline()

    assert ratchet.run([]) == 1
    err = capsys.readouterr().err
    assert "backend/core/thing.py::big: 82 lines" in err
    assert "over the 80-line standard for a new function" in err


def test_tracked_function_growth_fails_against_its_limit(ratchet, capsys):
    """Growth past a recorded function limit is a failure, not a new baseline."""
    ratchet.write_function("backend/core/thing.py", "big", 84)
    ratchet.write_baseline(function_limits={"backend/core/thing.py::big": 83})

    assert ratchet.run([]) == 1
    assert "85 lines, 2 more than its limit of 83" in capsys.readouterr().err


def test_function_under_its_recorded_limit_passes(ratchet, capsys):
    """A tolerated legacy function does not fail while it is worked down."""
    ratchet.write_function("backend/core/thing.py", "big", 84)
    ratchet.write_baseline(function_limits={"backend/core/thing.py::big": 85})

    assert ratchet.run([]) == 0
    assert "none over their limit" in capsys.readouterr().out


def test_shrunk_and_deleted_functions_require_baseline_tightening(ratchet, capsys):
    """A split must lower the limit, or the function can silently grow back."""
    ratchet.write_function("backend/core/thing.py", "big", 81)
    ratchet.write_baseline(
        function_limits={
            "backend/core/thing.py::big": 120,
            "backend/core/thing.py::removed": 90,
        }
    )

    assert ratchet.run([]) == 1
    out = capsys.readouterr().out
    assert '"backend/core/thing.py::big": 82,   (was 120)' in out
    assert "remove 'backend/core/thing.py::removed'" in out


def test_function_identities_are_scope_based_not_positional(ratchet):
    """Identities survive line movement and disambiguate real duplicates."""
    import size_limit_scan

    ratchet.write_source(
        "backend/core/scopes.py",
        "class First:\n    def dup(self):\n        return 1\n"
        "class Second:\n    def dup(self):\n        return 2\n"
        "def outer():\n    def inner():\n        return 3\n    return inner\n"
        "def same():\n    pass\ndef same():\n    pass\n",
    )

    _sizes, functions, _unreadable, _parse_errors = size_limit_scan.measure(
        ratchet.root
    )

    assert set(functions) == {
        "backend/core/scopes.py::First.dup",
        "backend/core/scopes.py::Second.dup",
        "backend/core/scopes.py::outer",
        "backend/core/scopes.py::outer.inner",
        "backend/core/scopes.py::same",
        "backend/core/scopes.py::same#2",
    }


def test_unparseable_python_fails_instead_of_vanishing(ratchet, capsys):
    """A syntax error is a gate failure, not a silently unmeasured file."""
    ratchet.write_source("backend/core/broken.py", "def nope(:\n")
    ratchet.write_baseline()

    assert ratchet.run([]) == 1
    assert "Python syntax error" in capsys.readouterr().err




def test_append_only_file_exempts_its_functions_too(ratchet, capsys):
    """A file with no legitimate ceiling cannot have its parts ratcheted."""
    ratchet.write_function("backend/infra/database_migrations.py", "migrate", 200)
    ratchet.write_baseline(
        append_only={
            "backend/infra/database_migrations.py": "append-only history by design"
        }
    )

    assert ratchet.run([]) == 0
    assert "none over their limit" in capsys.readouterr().out


# --- function write guards ------------------------------------------------


def test_write_refuses_new_oversized_function_without_adopt(ratchet, capsys):
    """Running the printed remediation cannot launder new function debt in."""
    ratchet.write_function("backend/core/thing.py", "big", 81)
    ratchet.write_baseline()

    assert ratchet.run(["--write"]) == 1
    err = capsys.readouterr().err
    assert "refusing to raise limits without --adopt" in err
    assert "backend/core/thing.py::big" in err
    assert "backend/core/thing.py::big" not in ratchet.read_baseline().get(
        "function_limits", {}
    )


def test_write_adopt_records_a_new_oversized_function(ratchet):
    """Adoption is deliberate and lands in the same baseline file."""
    ratchet.write_function("backend/core/thing.py", "big", 81)
    ratchet.write_baseline()

    assert ratchet.run(["--write", "--adopt"]) == 0
    assert ratchet.read_baseline()["function_limits"] == {
        "backend/core/thing.py::big": 82
    }


def test_adopt_does_not_sweep_in_unrelated_function_growth(ratchet, capsys):
    """Adopting a new function must not also raise a grown one by accident."""
    ratchet.write_function("backend/core/fresh.py", "fresh", 81)
    ratchet.write_function("backend/core/aged.py", "aged", 90)
    ratchet.write_baseline(function_limits={"backend/core/aged.py::aged": 85})

    assert ratchet.run(["--write", "--adopt"]) == 1
    err = capsys.readouterr().err
    assert "both brand-new over-standard" in err
    assert "backend/core/fresh.py::fresh" in err
    assert "backend/core/aged.py::aged" in err
    baseline = ratchet.read_baseline().get("function_limits", {})
    assert "backend/core/fresh.py::fresh" not in baseline
    assert baseline.get("backend/core/aged.py::aged") == 85


def test_function_shrink_is_recorded_alongside_a_refused_new_function(ratchet):
    """A decomposition's shrink is never held hostage by an unsafe sibling."""
    ratchet.write_function("backend/core/shrunk.py", "small", 81)
    ratchet.write_function("backend/core/fresh.py", "fresh", 81)
    ratchet.write_baseline(function_limits={"backend/core/shrunk.py::small": 200})

    assert ratchet.run(["--write"]) == 1
    baseline = ratchet.read_baseline()["function_limits"]
    assert baseline["backend/core/shrunk.py::small"] == 82
    assert "backend/core/fresh.py::fresh" not in baseline


def test_write_preserves_the_append_only_map(ratchet):
    """--write never touches the reason-bearing exemptions it does not own."""
    ratchet.write("backend/core/small.py", 10)
    ratchet.write_baseline(
        append_only={"backend/infra/database_migrations.py": "by design"}
    )

    assert ratchet.run(["--write"]) == 0
    assert ratchet.read_baseline()["append_only"] == {
        "backend/infra/database_migrations.py": "by design"
    }


# --- measurement honesty and baseline hygiene -----------------------------


def test_non_python_source_is_sized_but_contributes_no_functions(ratchet, capsys):
    """TS/Svelte keep file limits; absent function spans beat guessed ones."""
    import size_limit_scan

    ratchet.write("frontend/src/lib/big.ts", 450)
    ratchet.write_source(
        "frontend/src/lib/fns.ts",
        "function outer() {\n  init(1);\n  if (ready) {\n    go();\n  }\n}\n",
    )
    _sizes, functions, unreadable, parse_errors = size_limit_scan.measure(
        ratchet.root
    )

    # No phantom `outer.init` from a preceding call, because nothing is guessed.
    assert functions == {}
    assert unreadable == [] and parse_errors == []

    ratchet.write_baseline()
    assert ratchet.run([]) == 1
    assert "frontend/src/lib/big.ts" in capsys.readouterr().err


def test_valid_typescript_never_fails_the_gate(ratchet, capsys):
    """Source a hand-rolled lexer used to choke on must not block anything."""
    ratchet.write_source(
        "frontend/src/lib/regex.ts",
        "function f(s: string) {\n"
        "  if (/[{]/.test(s)) return 1;\n"
        "  const t = `a ${s} b`;\n"
        "  return typeof /'/ === 'object' ? 2 : 3;\n"
        "}\n",
    )
    ratchet.write_baseline()

    assert ratchet.run([]) == 0
    assert "none over their limit" in capsys.readouterr().out


def test_unparseable_file_does_not_report_its_functions_as_deleted(ratchet, capsys):
    """The printed remediation must never discard real baseline debt."""
    ratchet.write_source("backend/core/broken.py", "def nope(:\n")
    ratchet.write_baseline(function_limits={"backend/core/broken.py::nope": 90})

    assert ratchet.run([]) == 1

    captured = capsys.readouterr()
    assert "Python syntax error" in captured.err
    assert "no longer exists" not in captured.out
    assert "no longer exists" not in captured.err


def test_deleted_file_also_drops_its_function_limits(ratchet, capsys):
    """Orphaned entries would grant a legacy ceiling if the path returned."""
    ratchet.write_baseline(
        limits={"backend/core/gone.py": 500},
        function_limits={"backend/core/gone.py::vanished": 120},
    )

    assert ratchet.run([]) == 1

    out = capsys.readouterr().out
    assert "remove 'backend/core/gone.py'   (file no longer exists)" in out
    assert "remove 'backend/core/gone.py::vanished'" in out


def test_conftest_fixture_modules_are_not_production_source():
    """Narrowing the shared rule would promote fixtures to governed source."""
    import size_limit_scan

    for relative in (
        "backend/conftest.py",
        "backend/conftest_fixtures.py",
        "tools/qh/conftest_helpers.py",
    ):
        assert size_limit_scan.is_test_path(relative)
        assert not size_limit_scan.is_source(relative)


def test_adding_a_long_function_to_a_tracked_oversized_file_is_resolvable(ratchet):
    """The two dimensions are judged apart, or the escape hatch is unreachable.

    A shrink is recorded exactly, so a tracked oversized file's limit equals
    its current length. Adding an over-standard function therefore grows the
    file BY adding the function: one new function entry and one grown file
    entry describing a single event. Judged together, --adopt could never
    resolve it and the printed remediation would be impossible to follow.
    """
    ratchet.write_source(
        "backend/core/tracked.py", "x = 1\n" * 401 + "def added():\n" + "    pass\n" * 90
    )
    size = len((ratchet.root / "backend/core/tracked.py").read_text().splitlines())
    ratchet.write_baseline(limits={"backend/core/tracked.py": 401})

    assert ratchet.run(["--write", "--adopt"]) == 0

    baseline = ratchet.read_baseline()
    assert baseline["limits"]["backend/core/tracked.py"] == size
    assert baseline["function_limits"]["backend/core/tracked.py::added"] == 91


def test_one_dimension_being_unsafe_still_blocks_the_run(ratchet, capsys):
    """Independent judging must not become a way to launder debt per dimension."""
    ratchet.write("backend/core/fresh.py", 450)
    ratchet.write("backend/core/aged.py", 405)
    ratchet.write_baseline(limits={"backend/core/aged.py": 402})

    assert ratchet.run(["--write", "--adopt"]) == 1

    err = capsys.readouterr().err
    assert "both brand-new over-standard file(s)" in err
    baseline = ratchet.read_baseline()["limits"]
    assert "backend/core/fresh.py" not in baseline
    assert baseline["backend/core/aged.py"] == 402


def test_skipped_directories_are_exempt_and_the_exemption_is_explicit():
    """_SKIP_PARTS silently exempts tracked source, so state it in a test.

    Discovery is `git ls-files`, so tracked files under these names really can
    reach the scan -- unlike an untracked `.venv`. Vendored, generated, and
    coverage output are exempt on purpose: they are not hand-maintained source
    and a limit on them would block work no one can act on. That is a policy
    choice, and it must be visible rather than buried in a frozenset.
    """
    import size_limit_scan

    for part in ("generated", "vendor", "coverage", "dist", "build", "node_modules"):
        assert not size_limit_scan.is_source(f"frontend/src/{part}/thing.ts"), part
        assert not size_limit_scan.is_source(f"{part}/thing.py"), part


def test_a_directory_merely_containing_a_skipped_name_is_still_governed():
    """The skip is per path segment, not a substring match.

    A substring rule would exempt real source in directories like
    `vendored_adapters/` or `coverage_report_builder/`.
    """
    import size_limit_scan

    assert size_limit_scan.is_source("backend/vendored_adapters/client.py")
    assert size_limit_scan.is_source("backend/coverage_report_builder/run.py")
    assert size_limit_scan.is_source("frontend/src/generated_names/map.ts")


def test_is_test_relativises_absolute_paths_before_classifying():
    """is_test_path's repo-relative precondition must be enforced, not assumed.

    The collectors satisfy it only by convention (they walk relative roots from
    the repo root). An absolute path would let a checkout or CI-workspace
    directory named `tests` anywhere above the repository classify every file
    under it as test-only, silently dropping real source from the metrics.
    """
    import os
    import sys

    sys.path.insert(0, "tools/codebase-health")
    import metric_utils

    assert metric_utils.is_test("backend/tests/x.py")
    assert not metric_utils.is_test("backend/core/x.py")
    assert not metric_utils.is_test(os.path.join(os.getcwd(), "backend/core/x.py"))
