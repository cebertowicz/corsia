"""Tests for the frontend design-token compliance ratchet."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def token_ratchet(tmp_path, monkeypatch):
    import design_token_ratchet as module

    monkeypatch.setattr(module, "_ROOT", tmp_path)
    monkeypatch.setattr(module, "_BASELINE", tmp_path / "quality" / "design-token-ratchet.json")

    def write(rel_path: str, text: str) -> None:
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def baseline(files: dict[str, dict[str, int]]) -> None:
        module._BASELINE.parent.mkdir(parents=True, exist_ok=True)
        module._BASELINE.write_text(
            json.dumps({"_comment": ["test"], "files": files}), encoding="utf-8"
        )

    return module, write, baseline


def counts(**values: int) -> dict[str, int]:
    return {
        "raw_colors": values.get("raw_colors", 0),
        "raw_px": values.get("raw_px", 0),
        "heavy_borders": values.get("heavy_borders", 0),
        "raw_shadows": values.get("raw_shadows", 0),
        "row_menu_buttons": values.get("row_menu_buttons", 0),
    }


def test_equal_counts_pass(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/Card.svelte"
    write(path, "<style>.card { color: #fff; }</style>")
    baseline({path: counts(raw_colors=1)})

    assert module.main([]) == 0
    assert "above baseline" in capsys.readouterr().out


def test_category_rise_fails_with_source_and_hint(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/Card.svelte"
    write(path, "<style>.card { color: #fff; background: #000; }</style>")
    baseline({path: counts(raw_colors=1)})

    assert module.main([]) == 1
    err = capsys.readouterr().err
    assert f"{path}: raw_colors increased" in err
    assert "background: #000" in err
    assert "semantic colour token" in err


def test_drop_passes_and_reports_a_baseline_update(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/Card.svelte"
    write(path, "<style>.card { color: var(--text-primary); }</style>")
    baseline({path: counts(raw_colors=1)})

    assert module.main([]) == 0
    assert (
        f"design-token debt is below baseline in: {path}; "
        "run --update-baseline to record the reductions"
    ) in capsys.readouterr().out


def test_update_baseline_only_lowers_counts(token_ratchet):
    module, write, baseline = token_ratchet
    path = "frontend/src/Card.svelte"
    write(path, "<style>.card { color: #fff; }</style>")
    baseline({path: counts(raw_colors=2)})

    assert module.main(["--update-baseline"]) == 0
    assert module.load(module._BASELINE)[path]["raw_colors"] == 1


def test_update_baseline_refuses_to_raise_counts(token_ratchet):
    module, write, baseline = token_ratchet
    path = "frontend/src/Card.svelte"
    write(path, "<style>.card { color: #fff; background: #000; }</style>")
    baseline({path: counts(raw_colors=1)})

    assert module.main(["--update-baseline"]) == 1
    assert module.load(module._BASELINE)[path]["raw_colors"] == 1


def test_update_baseline_removes_a_deleted_file(token_ratchet):
    module, _write, baseline = token_ratchet
    baseline({"frontend/src/Removed.svelte": counts(raw_colors=1)})

    assert module.main(["--update-baseline"]) == 0
    assert module.load(module._BASELINE) == {}


def test_new_file_with_violations_fails(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/NewCard.svelte"
    write(path, "<style>.card { padding: 8px; }</style>")
    baseline({})

    assert module.main([]) == 1
    assert f"{path}: raw_px increased" in capsys.readouterr().err


def test_app_tokens_css_is_exempt(token_ratchet):
    module, write, baseline = token_ratchet
    write("frontend/src/app-tokens.css", ":root { --accent: #fff; --space-2: 8px; }")
    baseline({})

    assert module.main([]) == 0


def test_numeric_var_fallback_is_exempt_but_color_fallback_is_not(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/Card.svelte"
    write(
        path,
        "<style>.card { padding: var(--space-2, 8px); color: var(--ink, #fff); }</style>",
    )
    baseline({path: counts(raw_colors=1)})

    assert module.main([]) == 0
    assert "" == capsys.readouterr().err


def test_literal_ellipsis_button_fails(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/Row.svelte"
    write(path, "<button type=\"button\">⋯</button>")
    baseline({})

    assert module.main([]) == 1
    assert f"{path}: row_menu_buttons increased" in capsys.readouterr().err


def test_more_actions_after_arrow_handler_fails(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/Row.svelte"
    write(path, '<button onclick={() => open()} aria-label="More actions"></button>')
    baseline({})

    assert module.main([]) == 1
    assert f"{path}: row_menu_buttons increased" in capsys.readouterr().err


def test_ordinary_ellipsis_labels_pass(token_ratchet):
    module, write, baseline = token_ratchet
    path = "frontend/src/Status.svelte"
    write(path, "<button>Saving…</button><button>Browse…</button>")
    baseline({})

    assert module.main([]) == 0


def test_entities_and_issue_references_are_not_colours(token_ratchet):
    module, write, baseline = token_ratchet
    write("frontend/src/Message.svelte", "<p>&#9613; PR #919</p>")
    write("frontend/src/notes.ts", 'const note = "corsia#919; issue #920";')
    baseline({})

    assert module.main([]) == 0


def test_typescript_comments_do_not_open_strings(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/notes.ts"
    write(
        path,
        "/** The thread's plan (#1949). */\n"
        "// Keyed by the thread's id (#2269)\n"
        "const ink = '#fff';\n",
    )
    baseline({path: counts(raw_colors=1)})

    assert module.main([]) == 0
    assert "" == capsys.readouterr().err


def test_markup_style_colour_still_fails(token_ratchet, capsys):
    module, write, baseline = token_ratchet
    path = "frontend/src/Swatch.svelte"
    write(path, '<div style="color: #fff">swatch</div>')
    baseline({})

    assert module.main([]) == 1
    assert f"{path}: raw_colors increased" in capsys.readouterr().err
