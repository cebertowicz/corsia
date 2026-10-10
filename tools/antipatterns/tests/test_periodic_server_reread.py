"""Tests for `periodic-server-reread` rule."""

from __future__ import annotations

from pathlib import Path

_RULE = "periodic-server-reread"
_PATH = "frontend/src/lib/example.ts"
_FIXTURES = Path(__file__).parent / "fixtures" / "periodic_server_reread"


def _run(run_engine, source, path=_PATH):
    return run_engine(path, source, rule_id=_RULE)


def test_interval_with_inline_fetch_fires(run_engine):
    """A setInterval whose callback calls fetch() is a poll."""
    src = (
        "export function start() {\n"
        "\tsetInterval(async () => {\n"
        "\t\tconst r = await fetch('/api/x');\n"
        "\t\tvoid r;\n"
        "\t}, 5000);\n"
        "}\n"
    )
    findings = _run(run_engine, src)
    assert [f.line for f in findings] == [2]
    assert findings[0].severity == "error"


def test_interval_calling_named_load_function_fires(run_engine):
    """The callback may call a load helper by name."""
    src = (
        "const t = setInterval(() => void loadThreads(), 30_000);\n"
    )
    assert len(_run(run_engine, src)) == 1


def test_interval_passing_named_refresh_function_fires(run_engine):
    """The callback may be the helper itself."""
    src = "const id = setInterval(loadServerVersion, 60_000);\n"
    assert len(_run(run_engine, src)) == 1


def test_interval_calling_method_helper_fires(run_engine):
    """`options.pollSystem()` matches by its final identifier."""
    src = (
        "interval = setInterval(() => {\n"
        "\toptions.pollSystem();\n"
        "}, 30000);\n"
    )
    assert len(_run(run_engine, src)) == 1


def test_interval_calling_same_file_function_that_fetches_fires(run_engine):
    """A callback that names a same-file function inherits its server read."""
    src = (
        "async function tick() {\n"
        "\tawait fetch('/api/x');\n"
        "}\n"
        "setInterval(() => void tick(), 1000);\n"
    )
    findings = _run(run_engine, src)
    assert [f.line for f in findings] == [4]


def test_api_read_helper_fires(run_engine):
    """The api module's read helpers count as server reads."""
    src = "setInterval(() => void readPlan('t'), 1000);\n"
    assert len(_run(run_engine, src)) == 1


def test_self_rescheduling_timeout_poll_fires(run_engine):
    """A setTimeout whose named callback re-arms itself is a poll."""
    src = (
        "let handle;\n"
        "async function pollTick() {\n"
        "\tawait pollJobs();\n"
        "\thandle = setTimeout(pollTick, 2000);\n"
        "}\n"
        "handle = setTimeout(pollTick, 2000);\n"
    )
    assert [f.line for f in _run(run_engine, src)] == [4, 6]


def test_inline_self_rescheduling_timeout_fires(run_engine):
    """An inline callback that re-arms the timer and reads is a poll."""
    src = (
        "function arm() {\n"
        "\tsetTimeout(() => {\n"
        "\t\tvoid refreshAll();\n"
        "\t\tsetTimeout(arm, 1000);\n"
        "\t}, 1000);\n"
        "}\n"
    )
    assert 2 in [f.line for f in _run(run_engine, src)]


def test_svelte_file_fires(run_engine):
    """Svelte components are in scope."""
    src = (
        "<script lang=\"ts\">\n"
        "\tlet t = setInterval(() => void refreshDrain(), 1000);\n"
        "</script>\n"
    )
    assert len(_run(run_engine, src, "frontend/src/lib/components/A.svelte")) == 1


def test_clock_tick_does_not_fire(run_engine):
    """A pure local clock is not a poll."""
    src = (
        "const timer = setInterval(() => set(new Date()), 60_000);\n"
        "const t2 = setInterval(() => { clock = Date.now(); }, 10_000);\n"
    )
    assert _run(run_engine, src) == []


def test_elapsed_counter_does_not_fire(run_engine):
    """An elapsed-seconds counter is local state."""
    src = (
        "lifecycleTimer = setInterval(() => {\n"
        "\tlifecycleElapsedSeconds = Math.floor((Date.now() - startedAt) / 1000);\n"
        "}, 1_000);\n"
    )
    assert _run(run_engine, src) == []


def test_one_shot_deadline_refresh_does_not_fire(run_engine):
    """One setTimeout at a known deadline that re-reads once is not periodic."""
    src = (
        "function scheduleNextOverdue(next: number) {\n"
        "\tconst delay = Math.max(5000, next - Date.now());\n"
        "\toverdueTimer = setTimeout(() => {\n"
        "\t\toverdueTimer = null;\n"
        "\t\tvoid load([...listed]);\n"
        "\t}, delay);\n"
        "}\n"
        "async function load(ids: string[]) { await loadChunk(ids); }\n"
    )
    assert _run(run_engine, src) == []


def test_debounced_reload_does_not_fire(run_engine):
    """A debounce re-reads once per trigger."""
    src = "reloadT = setTimeout(() => void loadLibrary(), 400);\n"
    assert _run(run_engine, src) == []


def test_local_self_rescheduling_timeout_does_not_fire(run_engine):
    """A self-rescheduling timer with no server read is a local clock."""
    src = (
        "function schedule() {\n"
        "\ttimer = setTimeout(schedule, msUntilNextLocalDay(now));\n"
        "}\n"
    )
    assert _run(run_engine, src) == []


def test_timer_in_comment_or_string_does_not_fire(run_engine):
    """Comments and string contents are not code."""
    src = (
        "// setInterval(() => fetch('/x'), 1000)\n"
        "const s = \"setInterval(() => fetch('/x'), 1000)\";\n"
    )
    assert _run(run_engine, src) == []


def test_test_files_are_excluded(run_engine):
    """`*.test.ts` and `*.spec.ts` are not scanned."""
    src = "setInterval(() => void fetch('/x'), 1000);\n"
    assert _run(run_engine, src, "frontend/src/lib/a.test.ts") == []
    assert _run(run_engine, src, "frontend/src/lib/a.spec.ts") == []


def test_outside_frontend_src_is_out_of_scope(run_engine):
    """The rule only covers frontend/src."""
    src = "setInterval(() => void fetch('/x'), 1000);\n"
    assert _run(run_engine, src, "frontend/e2e/support/a.ts") == []
    assert _run(run_engine, src, "backend/a.ts") == []


def test_known_poller_paths_are_excluded(run_engine):
    """Each known poller is exempted by path; the same code elsewhere fires."""
    src = "setInterval(() => void refreshDrain(), 1000);\n"
    excluded = [
        "frontend/src/lib/homeLifecycle.ts",
        "frontend/src/lib/components/VersionOverlay.svelte",
        "frontend/src/lib/components/StatusBanners.svelte",
        "frontend/src/lib/components/v7/ProviderModelsBody.svelte",
        "frontend/src/lib/components/HostAccessLeaseControl.svelte",
    ]
    for path in excluded:
        assert _run(run_engine, src, path) == [], path
    assert len(_run(run_engine, src, "frontend/src/lib/components/Other.svelte")) == 1


def test_removed_plan_state_polls_fire(run_engine):
    """The plan-state polls removed in #155 are caught by the rule."""
    for name, path in (
        ("threadPlanStates.ts.txt", "frontend/src/lib/stores/threadPlanStates.ts"),
        ("planQuery.svelte.ts.txt", "frontend/src/lib/stores/planQuery.svelte.ts"),
    ):
        findings = _run(run_engine, (_FIXTURES / name).read_text(encoding="utf-8"), path)
        assert len(findings) == 1, name
        assert "setInterval" in findings[0].snippet or "setInterval" in findings[0].message


_BARE_CALLS = ["planQuery.refresh()", "void load()", "store.reload()", "poll()", "x.sync()"]


def test_bare_verb_calls_in_setinterval_fire(run_engine):
    """Store-method polls (`x.refresh()`, `load()`, `store.reload()`) fire."""
    for call in _BARE_CALLS:
        src = f"setInterval(() => {{ {call}; }}, 30000);\n"
        assert len(_run(run_engine, src)) == 1, call
    assert len(_run(run_engine, "setInterval(() => planQuery.refresh(), 45000);\n")) == 1


def test_bare_verb_calls_in_self_rescheduling_timeout_fire(run_engine):
    """The same bare calls fire inside a self-rescheduling setTimeout."""
    for call in _BARE_CALLS:
        src = (
            "function arm() {\n"
            f"\tsetTimeout(() => {{ {call}; arm(); }}, 30000);\n"
            "}\n"
        )
        assert [f.line for f in _run(run_engine, src)] == [2], call


def test_bare_verb_calls_in_one_shot_timeout_do_not_fire(run_engine):
    """A one-shot timeout calling `refresh()` is a deadline, not a poll."""
    src = "setTimeout(() => { store.refresh(); }, 400);\n"
    assert _run(run_engine, src) == []


def test_bare_verb_prefix_of_other_word_does_not_fire(run_engine):
    """`loader()` and `syncing()` are not the bare verbs."""
    src = "setInterval(() => { loader(); syncing(); polled(); }, 1000);\n"
    assert _run(run_engine, src) == []


def test_clock_next_to_bare_verb_name_does_not_fire(run_engine):
    """A local clock tick that only sets state stays clean."""
    src = "setInterval(() => { now = Date.now(); }, 1000);\n"
    assert _run(run_engine, src) == []


_LOCAL_HELPERS = ["refreshRelativeTimes", "syncScrollPosition", "loadFromCache", "pollLocalDraft"]


def test_local_verb_named_helpers_do_not_fire(run_engine):
    """A same-file helper with a local-only body is judged by its body."""
    for name in _LOCAL_HELPERS:
        src = (
            f"function {name}() {{ label = format(Date.now()); }}\n"
            f"setInterval(() => {name}(), 1000);\n"
            f"setInterval({name}, 1000);\n"
        )
        assert _run(run_engine, src) == [], name


def test_same_names_with_server_read_body_fire(run_engine):
    """The same names fire when the body calls fetch( or readPlan(."""
    for name in _LOCAL_HELPERS:
        for read in ("await fetch('/api/x');", "await readPlan('t');"):
            src = (
                f"async function {name}() {{ {read} }}\n"
                f"setInterval(() => void {name}(), 1000);\n"
            )
            assert len(_run(run_engine, src)) == 1, (name, read)


def test_two_level_chain_fires(run_engine):
    """`refresh()` -> `doLoad()` -> `fetch(` is still caught."""
    src = (
        "async function doLoad() { await fetch('/api/x'); }\n"
        "function refresh() { void doLoad(); }\n"
        "setInterval(() => refresh(), 1000);\n"
    )
    assert [f.line for f in _run(run_engine, src)] == [3]


def test_long_chain_is_followed_and_cycles_terminate(run_engine):
    """A five-hop chain to fetch( is flagged; mutual recursion terminates."""
    cyc = (
        "function a() { b(); }\nfunction b() { a(); }\n"
        "setInterval(() => a(), 1000);\n"
    )
    assert _run(run_engine, cyc) == []
    deep = (
        "async function d5() { await fetch('/x'); }\n"
        "function d4() { d5(); }\nfunction d3() { d4(); }\n"
        "function d2() { d3(); }\nfunction d1() { d2(); }\n"
        "setInterval(() => d1(), 1000);\n"
    )
    assert len(_run(run_engine, deep)) == 1


def test_member_call_is_never_resolved_locally(run_engine):
    """`planQuery.refresh` is name-tested even if a local `refresh` is clean."""
    local = "function refresh() { label = Date.now(); }\n"
    assert _run(run_engine, local + "setInterval(() => refresh(), 45000);\n") == []
    assert len(_run(run_engine, local + "setInterval(planQuery.refresh, 45000);\n")) == 1
    member = "setInterval(() => planQuery.refresh(), 45000);\n"
    assert len(_run(run_engine, local + member)) == 1


def test_unresolved_member_refresh_still_fires(run_engine):
    """`planQuery.refresh()` has no same-file body, so the name test applies."""
    assert len(_run(run_engine, "setInterval(() => planQuery.refresh(), 45000);\n")) == 1


def test_allow_listed_file_is_not_flagged(run_engine):
    """A reviewed allow_files entry is skipped; the same code elsewhere fires."""
    src = "setInterval(() => void refreshDrain(), 1000);\n"
    path = "frontend/src/lib/components/Accepted.svelte"
    overlay = {"rules": {_RULE: {"allow_files": [path]}}}
    assert run_engine(path, src, rule_id=_RULE, config_overlay=overlay) == []
    assert len(_run(run_engine, src, "frontend/src/lib/components/Other.svelte")) == 1


def test_residual_class_method_helper_is_flagged(run_engine):
    """Known residual: a verb-named class method the resolver cannot see."""
    src = (
        "class Store { refresh() { this.t = Date.now(); } }\n"
        "const store = new Store();\n"
        "setInterval(() => store.refresh(), 1000);\n"
    )
    assert len(_run(run_engine, src)) == 1


def test_config_lists_name_existing_files():
    """allow_files entries are exact existing paths; exclude_globs match a file."""
    import tomllib

    repo = Path(__file__).resolve().parents[3]
    cfg = tomllib.loads(
        (repo / "tools" / "antipatterns" / "default-config.toml").read_text(encoding="utf-8")
    )
    section = cfg["rules"][_RULE]
    assert section["allow_files"] and section["exclude_globs"]
    missing = [e for e in section["allow_files"] if not (repo / e).is_file()]
    assert missing == []
    dead = [g for g in section["exclude_globs"] if not any(p.is_file() for p in repo.glob(g))]
    assert dead == []
