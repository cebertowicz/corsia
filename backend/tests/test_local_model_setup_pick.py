"""Setup's automatic pick fits the memory free now, the Add model sheet's rule."""

from __future__ import annotations

import asyncio

from llm import local_model
from llm.local_model import LocalModel, pick_for_setup

GIB = 1024**3


def _model(name: str, resident_gib: float | None) -> LocalModel:
    resident = None if resident_gib is None else int(resident_gib * GIB)
    return LocalModel(f"mlx-community/{name}", 10 * GIB, 262_144, resident_bytes=resident)


def test_the_first_best_row_when_it_fits():
    best = [_model("big", 100), _model("small", 20)]
    assert pick_for_setup({"best": best}, 115 * GIB) == best[0]


def test_a_best_row_too_big_for_free_memory_is_skipped():
    # The observed case: 163 GB needed with 115 GB free.
    best = [_model("deepseek", 163), _model("glm", 182), _model("qwen", 99)]
    assert pick_for_setup({"best": best}, 115 * GIB).model == "qwen"


def test_other_sections_follow_best_and_unlock_is_never_picked():
    sections = {
        "unlock": [_model("needs-more-mac", 10)],
        "best": [_model("deepseek", 163)],
        "faster": [_model("ornith", 21)],
    }
    assert pick_for_setup(sections, 115 * GIB).model == "ornith"


def test_nothing_fits_picks_the_smallest_row():
    sections = {"best": [_model("deepseek", 163)], "faster": [_model("qwen", 40), _model("ornith", 21)]}
    assert pick_for_setup(sections, 8 * GIB).model == "ornith"


def test_a_row_without_a_loaded_size_counts_as_fitting():
    best = [_model("unsized", None), _model("small", 4)]
    assert pick_for_setup({"best": best}, 2 * GIB).model == "unsized"


def test_no_rows_is_no_pick():
    assert pick_for_setup({"best": [], "unlock": [_model("x", 1)]}, 64 * GIB) is None


def test_ask_recommender_compares_against_available_memory(monkeypatch):
    async def recommendations(_server_id: str) -> dict[str, list[LocalModel]]:
        return {"best": [_model("deepseek", 163), _model("qwen", 99)]}

    monkeypatch.setattr(local_model, "ask_recommendations", recommendations)
    monkeypatch.setattr("infra.host_resources.host_available_ram_bytes", lambda: 115 * GIB)
    assert asyncio.run(local_model.ask_recommender("server")).model == "qwen"
