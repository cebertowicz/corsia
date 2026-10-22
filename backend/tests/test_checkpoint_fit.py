"""One boundary decision and one deterministic cold checkpoint fit ladder."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from history.types import (
    PORTABLE_HISTORY_FORMAT_VERSION,
    HistoryBoundary,
    PortableHistoryProjection,
    PortableMessage,
    PortableTurn,
)


THREAD = "checkpoint-fit-thread"


def _message(seq: int, role: str, content: str, **values) -> PortableMessage:
    return PortableMessage(
        message_id=values.pop("message_id", f"message-{seq}"),
        seq=seq,
        turn_id=values.pop("turn_id", "turn-1"),
        role=role,
        content=content,
        created_at=f"2026-08-26T00:00:{seq:02d}Z",
        **values,
    )


def _turn(index: int, messages: tuple[PortableMessage, ...]) -> PortableTurn:
    boundary_message = max(messages, key=lambda message: message.seq)
    return PortableTurn(
        turn_id=f"turn-{index}",
        status="completed",
        messages=tuple(
            replace(message, turn_id=f"turn-{index}") for message in messages
        ),
        boundary=HistoryBoundary(
            THREAD,
            1,
            boundary_message.seq,
            boundary_message.message_id,
        ),
    )


def _projection(*turns: PortableTurn) -> PortableHistoryProjection:
    return PortableHistoryProjection(
        format_version=PORTABLE_HISTORY_FORMAT_VERSION,
        thread_id=THREAD,
        history_epoch=1,
        start_exclusive=None,
        sealed_boundary=turns[-1].boundary,
        turns=turns,
        full_raw_replay_available=True,
    )


def _decision(payload) -> str:
    from core.fork_fit import evaluate_boundary

    result = evaluate_boundary(payload)
    return getattr(result, "value", result)


def test_correlated_live_usage_wins_over_the_canonical_upper_bound() -> None:
    from core.fork_fit import ProviderPayload

    payload = ProviderPayload(
        context_window=262_144,
        input_tokens=200_000,
        canonical_input_tokens=270_000,
        minimum_tokens=8_000,
        checkpoint_status=None,
        installed_input_tokens=None,
        checkpoint_admissible=True,
    )

    assert _decision(payload) == "proceed"


def test_missing_usage_uses_the_canonical_rendered_upper_bound() -> None:
    from core.fork_fit import ProviderPayload

    payload = ProviderPayload(
        context_window=262_144,
        input_tokens=None,
        canonical_input_tokens=237_000,
        minimum_tokens=8_000,
        checkpoint_status=None,
        installed_input_tokens=None,
        checkpoint_admissible=True,
    )

    assert _decision(payload) == "proceed_and_fold"


def test_canonical_rendered_count_is_a_required_programmer_contract() -> None:
    from core.fork_fit import ProviderPayload

    with pytest.raises(TypeError):
        ProviderPayload(
            context_window=262_144,
            input_tokens=None,
            minimum_tokens=8_000,
            checkpoint_status=None,
            installed_input_tokens=None,
            checkpoint_admissible=True,
        )


def test_full_cold_input_uses_the_exact_ordinary_projection() -> None:
    from core.fork_fit import render_cold_fold_input

    source = _projection(
        _turn(1, (_message(1, "user", "first ask"), _message(2, "assistant", "first answer"))),
        _turn(2, (_message(3, "user", "second ask"), _message(4, "assistant", "second answer"))),
    )

    rendered = render_cold_fold_input(source, max_input_tokens=10_000)
    text = "\n".join(message["content"] for message in rendered.messages)

    assert rendered.first_included_seq == 1
    assert rendered.omitted_units == 0
    assert "first ask" in text and "first answer" in text
    assert "second ask" in text and "second answer" in text
    assert "omitted checkpoint source" not in text.lower()


def test_large_old_tool_arguments_and_results_project_once_without_omission() -> None:
    from core.fork_fit import render_cold_fold_input

    arguments = "ARG-HEAD-" + ("a" * 40_000) + "-ARG-TAIL"
    result = "RESULT-HEAD-" + ("r" * 40_000) + "-RESULT-TAIL"
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "lookup", "arguments": arguments},
    }
    source = _projection(
        _turn(
            1,
            (
                _message(1, "user", "inspect it"),
                _message(2, "assistant", "", tool_calls=(call,)),
                _message(
                    3,
                    "tool",
                    result,
                    assistant_step_id="message-2",
                    tool_call_id="call-1",
                    tool_name="lookup",
                ),
            ),
        ),
        _turn(2, (_message(4, "user", "finish"), _message(5, "assistant", "done"))),
    )
    exact = render_cold_fold_input(source, max_input_tokens=100_000)

    projected = render_cold_fold_input(
        source,
        max_input_tokens=exact.input_tokens - 1,
    )
    text = "\n".join(message["content"] for message in projected.messages)

    assert projected.omitted_units == 0
    assert "ARG-HEAD-" in text and "-ARG-TAIL" in text
    assert "arguments_sha256" in text and "arguments_byte_count" in text
    assert "message-2" in text
    assert "CORSIA_TOOL_RESULT_PREVIEW" in text
    assert "corsia://threads/" in text
    assert source.turns[0].messages[1].tool_calls[0]["function"]["arguments"] == arguments
    assert source.turns[0].messages[2].content == result


def test_default_checkpoint_source_render_never_projects_or_truncates() -> None:
    from history.adapters import to_checkpoint_source_messages

    arguments = "EXACT-ARGUMENT-" + ("a" * 8_000)
    result = "EXACT-RESULT-" + ("r" * 8_000)
    call = {
        "id": "call-exact",
        "type": "function",
        "function": {"name": "lookup", "arguments": arguments},
    }
    source = _projection(
        _turn(
            1,
            (
                _message(1, "user", "inspect"),
                _message(2, "assistant", "", tool_calls=(call,)),
                _message(
                    3,
                    "tool",
                    result,
                    assistant_step_id="message-2",
                    tool_call_id="call-exact",
                    tool_name="lookup",
                ),
            ),
        )
    )

    text = "\n".join(block["content"] for block in to_checkpoint_source_messages(source))

    assert arguments in text and result in text
    assert "arguments_sha256" not in text
    assert "CORSIA_TOOL_RESULT_PREVIEW" not in text


def test_checkpoint_tail_tool_arguments_stay_valid_and_exact_history_is_unchanged(
) -> None:
    from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS
    from history.adapters import to_native_history
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import codex_inject_items

    arguments = json.dumps({"path": "/tmp/" + ("nested/" * 10_000) + "file.txt"})
    call = {
        "id": "call-large",
        "type": "function",
        "function": {"name": "read_file", "arguments": arguments},
    }
    source = _projection(_turn(1, (
        _message(1, "user", "inspect"),
        _message(2, "assistant", "checking", tool_calls=(call,)),
    )))

    native_exact = to_native_history(source)
    native_tail = to_native_history(
        source,
        tool_argument_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
    )
    cc_exact = cc_snapshot_history(source, cc_session_id="cc-exact")
    cc_tail = cc_snapshot_history(
        source,
        cc_session_id="cc-tail",
        tool_argument_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
    )
    codex_exact = codex_inject_items(source)
    codex_tail = codex_inject_items(
        source,
        tool_argument_budget_tokens=HARNESS_TOOL_RESULT_BUDGET_TOKENS,
    )

    assert native_exact[-1]["tool_calls"][0]["function"]["arguments"] == arguments
    assert cc_exact[-1]["content"][-1]["input"] == json.loads(arguments)
    assert "file.txt" in codex_exact[-1]["content"][0]["text"]
    assert "arguments_sha256" not in codex_exact[-1]["content"][0]["text"]
    native_projected = native_tail[-1]["tool_calls"][0]
    cc_projected = cc_tail[-1]["content"][-1]["input"]
    codex_projected = codex_tail[-1]["content"][0]["text"]
    assert "arguments_sha256" in json.dumps(native_projected)
    assert "_corsia_checkpoint_argument_preview" in cc_projected
    assert "arguments_sha256" in codex_projected
    assert len(json.dumps(native_projected)) < len(arguments)
    assert len(json.dumps(cc_projected)) < len(arguments)
    assert len(codex_projected) < len(arguments)
    assert source.messages[-1].tool_calls[0]["function"]["arguments"] == arguments


@pytest.mark.parametrize("mode", ("exact", "checkpoint_tail"))
def test_native_projects_arguments_only_for_checkpoint_tail(
    monkeypatch,
    mode,
) -> None:
    from core import native_context_prefix as prefix
    from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS

    captured = {}

    def adapt(_tail, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("history.adapters.to_native_history", adapt)
    monkeypatch.setattr(
        "llm.capabilities.native_transport_identity",
        lambda _model: ("openai", "test"),
    )
    prefix._load_history(
        thread_id="thread-1",
        turn_model="native/test",
        fresh_context=False,
        replay_selection=SimpleNamespace(mode=mode, tail=object()),
    )

    expected = HARNESS_TOOL_RESULT_BUDGET_TOKENS if mode == "checkpoint_tail" else None
    assert captured["tool_argument_budget_tokens"] == expected


@pytest.mark.parametrize("mode", ("exact", "checkpoint_tail"))
def test_cc_projects_arguments_only_for_checkpoint_tail(monkeypatch, mode) -> None:
    from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS
    from subagents import cc_destination

    captured = {}
    from subagents.cc_invocation import UNCONFINED

    monkeypatch.setattr(cc_destination, "launch_confinement", lambda *_a: UNCONFINED)
    monkeypatch.setattr(cc_destination, "registered_cwd_or_default", lambda *_a: "/tmp")
    monkeypatch.setattr(cc_destination, "_main_working_dir", lambda *_a: "/tmp")
    monkeypatch.setattr(cc_destination, "_artifact_cc_session_id", lambda *_a: "cc-1")
    monkeypatch.setattr("subagents.harness_tool_bridge.build_tool_registry", lambda *_a: object())
    monkeypatch.setattr("subagents.harness_tool_bridge.dynamic_tool_definitions", lambda *_a: ())

    def adapt(_tail, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cc_destination, "cc_snapshot_history", adapt)
    monkeypatch.setattr(
        "core.context_replay.checkpoint_prompt",
        lambda _selection: "checkpoint" if mode == "checkpoint_tail" else "",
    )

    cc_destination._cold_material(
        "thread-1",
        selection=SimpleNamespace(mode=mode, tail=object()),
        target=SimpleNamespace(expected_reported_model="test"),
        project_id=None,
        claude_dir=None,
        db=object(),
    )

    expected = HARNESS_TOOL_RESULT_BUDGET_TOKENS if mode == "checkpoint_tail" else None
    assert captured["tool_argument_budget_tokens"] == expected


@pytest.mark.parametrize("mode", ("exact", "checkpoint_tail"))
def test_codex_projects_arguments_only_for_checkpoint_tail(monkeypatch, mode) -> None:
    from core.tool_result_projection import HARNESS_TOOL_RESULT_BUDGET_TOKENS
    from subagents import codex_destination_render as destination

    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "subagents.codex_context_reconstruction.build_codex_context_reconstruction",
        build,
    )
    destination._render_reconstruction(
        thread_id="thread-1",
        user_text="continue",
        user_source_text="continue",
        agent_envelope=None,
        target=SimpleNamespace(
            catalog_id="codex/test",
            context_window=100_000,
        ),
        project_id=None,
        working_dir="/tmp",
        prepared=SimpleNamespace(
            delivery_mode=mode,
            codex_thread_id=None,
            history_projection=object(),
            checkpoint_text=("checkpoint" if mode == "checkpoint_tail" else None),
        ),
        db=object(),
    )

    expected = HARNESS_TOOL_RESULT_BUDGET_TOKENS if mode == "checkpoint_tail" else None
    assert captured["tool_argument_budget_tokens"] == expected


def test_trimming_drops_only_whole_oldest_steps() -> None:
    from core.fork_fit import render_cold_fold_input

    source = _projection(
        _turn(1, (_message(1, "user", "OLD-A" * 2_000), _message(2, "assistant", "A-END"))),
        _turn(2, (_message(3, "user", "OLD-B" * 2_000), _message(4, "assistant", "B-END"))),
        _turn(3, (_message(5, "user", "CURRENT ASK"), _message(6, "assistant", "CURRENT ANSWER"))),
    )

    rendered = render_cold_fold_input(source, max_input_tokens=300)
    text = "\n".join(message["content"] for message in rendered.messages)

    assert rendered.first_included_seq == 5
    assert rendered.omitted_units == 2
    assert "OLD-A" not in text and "OLD-B" not in text
    assert "CURRENT ASK" in text and "CURRENT ANSWER" in text
    assert "omitted checkpoint source" in text.lower()


def test_mid_turn_resume_keeps_the_initiating_input_and_newest_exchange() -> None:
    from core.fork_fit import render_cold_fold_input

    first_call = {
        "id": "call-old",
        "type": "function",
        "function": {"name": "old_tool", "arguments": "x" * 20_000},
    }
    newest_call = {
        "id": "call-new",
        "type": "function",
        "function": {"name": "new_tool", "arguments": "small"},
    }
    source = _projection(
        _turn(
            1,
            (
                _message(1, "user", "ORIGINAL UNFINISHED ASK"),
                _message(2, "assistant", "OLD STEP", tool_calls=(first_call,)),
                _message(
                    3,
                    "tool",
                    "old-result-" + ("z" * 20_000),
                    assistant_step_id="message-2",
                    tool_call_id="call-old",
                    tool_name="old_tool",
                ),
                _message(4, "assistant", "LATEST STEP", tool_calls=(newest_call,)),
                _message(
                    5,
                    "tool",
                    "LATEST RESULT " + ("q" * 2_000),
                    assistant_step_id="message-4",
                    tool_call_id="call-new",
                    tool_name="new_tool",
                ),
            ),
        )
    )

    rendered = render_cold_fold_input(source, max_input_tokens=2_500)
    text = "\n".join(message["content"] for message in rendered.messages)

    assert rendered.first_included_seq == 4
    assert rendered.omitted_units == 1
    assert "resumes mid-turn" in text.lower()
    assert "ORIGINAL UNFINISHED ASK" in text
    assert "OLD STEP" not in text and "old-result" not in text
    assert "LATEST STEP" in text and "LATEST RESULT" in text


def test_existing_mid_turn_projection_gets_the_same_portable_resume_envelope() -> None:
    from core.fork_fit import render_cold_fold_input

    whole = _turn(
        1,
        (
            _message(1, "user", "already checkpointed input"),
            _message(2, "assistant", "earlier step"),
            _message(3, "assistant", "exact tail step"),
        ),
    )
    partial = replace(
        whole,
        messages=(whole.messages[-1],),
        includes_turn_start=False,
    )
    source = replace(
        _projection(partial),
        start_exclusive=HistoryBoundary(THREAD, 1, 2, "message-2"),
    )

    rendered = render_cold_fold_input(source, max_input_tokens=1_000)
    text = "\n".join(message["content"] for message in rendered.messages)

    assert rendered.omitted_units == 0
    assert rendered.first_included_seq == 3
    assert "resumes mid-turn" in text.lower()
    assert "exact tail step" in text


def test_installed_checkpoint_and_continuity_blocks_are_protected() -> None:
    from core.fork_fit import render_cold_fold_input

    source = _projection(
        _turn(1, (_message(1, "user", "OLD" * 5_000), _message(2, "assistant", "old"))),
        _turn(2, (_message(3, "user", "new ask"), _message(4, "assistant", "new answer"))),
    )
    protected = (
        {"role": "user", "content": "INSTALLED CHECKPOINT"},
        {"role": "user", "content": "ACTIVE CONTINUITY ENTRIES"},
    )

    rendered = render_cold_fold_input(
        source,
        max_input_tokens=350,
        protected_blocks=protected,
    )
    text = "\n".join(message["content"] for message in rendered.messages)

    assert "INSTALLED CHECKPOINT" in text
    assert "ACTIVE CONTINUITY ENTRIES" in text
    assert "new ask" in text and "new answer" in text


def test_fit_does_not_skip_a_turn_start_before_mid_turn_resume_overhead() -> None:
    """A larger mid-Turn suffix cannot hide an older fitting Turn boundary."""
    from core.fork_fit import render_cold_fold_input

    source = _projection(
        _turn(
            1,
            (
                _message(1, "user", "OLD" * 1_000),
                _message(2, "assistant", "old"),
            ),
        ),
        _turn(
            2,
            (
                _message(3, "user", "u"),
                _message(4, "assistant", ""),
                _message(5, "assistant", ""),
                _message(6, "assistant", "newest"),
            ),
        ),
    )

    rendered = render_cold_fold_input(source, max_input_tokens=115)

    assert rendered.first_included_seq == 3
    assert rendered.omitted_units == 1


def test_fit_does_not_skip_an_older_interior_cut_for_boundary_label_size() -> None:
    """Durable boundary identity must not distort cold-fold fit selection."""
    from core.fork_fit import render_cold_fold_input

    source = _projection(
        _turn(
            1,
            (
                _message(1, "user", "", message_id="u1x"),
                _message(2, "assistant", "OLD" * 1_000, message_id="a2x"),
                _message(3, "assistant", "", message_id="a3xxxxx"),
                _message(4, "assistant", "", message_id="a4x"),
                _message(5, "assistant", "", message_id="a5" + ("x" * 80)),
            ),
        )
    )

    rendered = render_cold_fold_input(source, max_input_tokens=233)

    assert rendered.first_included_seq == 3
    assert rendered.omitted_units == 1


@pytest.mark.parametrize("fixed_envelope_tokens", (0, 32, 64))
def test_large_cold_fit_uses_bounded_candidate_counts_and_keeps_oldest_fit(
    fixed_envelope_tokens,
) -> None:
    """A 10K-message fold selects five newest exchanges in bounded work."""
    from core.fork_fit import render_cold_fold_input

    turns = tuple(
        _turn(
            index,
            (
                _message(index * 2 - 1, "user", f"ask-{index:04d}"),
                _message(index * 2, "assistant", f"answer-{index:04d}"),
            ),
        )
        for index in range(1, 5_001)
    )
    calls = 0

    def count_tokens(messages) -> int:
        nonlocal calls
        calls += 1
        if calls > 20:
            raise AssertionError("cold fit recounted too many suffix candidates")
        return fixed_envelope_tokens + len(messages)

    rendered = render_cold_fold_input(
        _projection(*turns),
        max_input_tokens=fixed_envelope_tokens + 10,
        protected_blocks=({"role": "user", "content": "PROTECTED"},),
        count_tokens=count_tokens,
    )
    text = "\n".join(message["content"] for message in rendered.messages)

    assert calls <= 20
    assert rendered.first_included_seq == 9_991
    assert rendered.omitted_units == 4_995
    assert "ask-4996" in text and "answer-5000" in text
    assert "ask-4995" not in text
    assert "PROTECTED" in text


def test_impossible_protected_minimum_fails_visibly() -> None:
    from core.fork_fit import ColdFoldInputTooLarge, render_cold_fold_input

    source = _projection(
        _turn(
            1,
            (
                _message(1, "user", "required initiating input " + ("u" * 5_000)),
                _message(2, "assistant", "required newest response " + ("a" * 5_000)),
            ),
        )
    )

    with pytest.raises(ColdFoldInputTooLarge, match="minimum meaningful"):
        render_cold_fold_input(source, max_input_tokens=100)


def test_newest_tool_unit_retries_with_a_reduced_projection_budget() -> None:
    from core.fork_fit import render_cold_fold_input

    call = {
        "id": "call-newest",
        "type": "function",
        "function": {"name": "inspect", "arguments": "small"},
    }
    source = _projection(
        _turn(
            1,
            (
                _message(1, "user", "inspect the newest result"),
                _message(2, "assistant", "", tool_calls=(call,)),
                _message(
                    3,
                    "tool",
                    "NEWEST RESULT " + ("r" * 30_000),
                    assistant_step_id="message-2",
                    tool_call_id="call-newest",
                    tool_name="inspect",
                ),
            ),
        )
    )

    rendered = render_cold_fold_input(source, max_input_tokens=2_000)

    assert rendered.input_tokens <= 2_000
    assert rendered.first_included_seq is not None


@pytest.mark.parametrize("shape", ("px", "governance"))
def test_cold_fold_fits_large_tool_runs_with_reference_projection(shape: str) -> None:
    from core.fork_fit import render_cold_fold_input

    counts = (297,) if shape == "px" else (11,) * 4 + (10,) * 14 + (30,)
    body_size = 1_780 if shape == "px" else 3_835
    initiating = "Continue this work" if shape == "px" else "context " * 80
    messages = [_message(1810, "user", initiating, message_id="input")]
    next_seq = 1811
    for group_index, count in enumerate(counts):
        if shape == "governance" and group_index:
            messages.append(_message(
                next_seq, "user", "context " * 80,
                message_id=f"input-{group_index}",
            ))
            next_seq += 1
        step_id = f"step-{group_index}"
        calls = tuple({
            "id": f"call-{group_index}-{call_index}", "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        } for call_index in range(count))
        messages.append(_message(next_seq, "assistant", "", message_id=step_id,
                                 tool_calls=calls))
        next_seq += 1
        for call_index in range(count):
            call_id = f"call-{group_index}-{call_index}"
            body = (f"{call_id}: " + "sample result text " * 220)[:body_size]
            messages.append(_message(
                next_seq, "tool", body, assistant_step_id=step_id,
                tool_call_id=call_id, tool_name="read_file",
            ))
            next_seq += 1
    source = _projection(_turn(1, tuple(messages)))
    protected: tuple[dict[str, str], ...] = (
        {"role": "user", "content": "previous narrative" * 60},
    )
    surface = 12_000 if shape == "px" else 172_000
    if shape == "governance":
        protected = ({"role": "user", "content": "continuity " * 3_054},
                     {"role": "user", "content": "prior fold" * 110})

    def count_tokens(blocks) -> int:
        return surface + sum(len(block["content"].encode("utf-8")) + 8 for block in blocks)

    fitted = render_cold_fold_input(
        source, max_input_tokens=232_000,
        protected_blocks=protected, count_tokens=count_tokens,
    )
    assert fitted.input_tokens <= 232_000
    assert fitted.messages[0]["content"].startswith(protected[0]["content"])
    assert f"call-{len(counts) - 1}-{counts[-1] - 1}" in "\n".join(
        block["content"] for block in fitted.messages
    )


def test_cold_fold_stubs_old_results_when_even_references_exceed_writer_room() -> None:
    from core.fork_fit import render_cold_fold_input

    calls = tuple({
        "id": f"call-{index}", "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    } for index in range(297))
    messages = [
        _message(1, "user", "continue"),
        _message(2, "assistant", "", tool_calls=calls),
    ]
    messages.extend(_message(
        3 + index, "tool", "large body " * 200,
        assistant_step_id="message-2", tool_call_id=f"call-{index}",
        tool_name="read_file",
    ) for index in range(297))

    fitted = render_cold_fold_input(
        _projection(_turn(1, tuple(messages))), max_input_tokens=48_000,
    )
    content = "\n".join(block["content"] for block in fitted.messages)
    assert fitted.input_tokens <= 48_000
    assert "[result body omitted from this cold checkpoint input]" in content
    assert "call-296" in content
    assert "CORSIA_TOOL_RESULT_PREVIEW" in content


def test_protected_blocks_alone_over_budget_still_fail_visibly() -> None:
    from core.fork_fit import ColdFoldInputTooLarge, render_cold_fold_input

    source = _projection(_turn(1, (_message(1, "user", "small"),)))

    with pytest.raises(ColdFoldInputTooLarge, match="minimum meaningful"):
        render_cold_fold_input(
            source,
            max_input_tokens=100,
            protected_blocks=({"role": "user", "content": "PROTECTED " * 1_000},),
        )


def test_already_fitting_input_remains_the_exact_first_candidate() -> None:
    from core import fork_fit
    from history.folds import checkpoint_fold_units

    source = _projection(
        _turn(1, (_message(1, "user", "first ask"), _message(2, "assistant", "first answer")))
    )
    units = checkpoint_fold_units(source)
    expected = fork_fit._candidate(
        source,
        units,
        skipped=0,
        project_tools=False,
        newest_tool_budget_tokens=fork_fit.HARNESS_TOOL_RESULT_BUDGET_TOKENS,
        protected_blocks=(),
        count_tokens=fork_fit._estimate_messages_tokens,
    )

    rendered = fork_fit.render_cold_fold_input(source, max_input_tokens=10_000)

    assert expected is not None
    assert rendered.messages == expected.messages
    assert rendered.input_tokens == expected.input_tokens


def test_complete_final_turn_beats_a_harder_truncated_fallback() -> None:
    """Cutting into the final turn re-adds its initiating input untruncated, so
    candidate size does not fall monotonically with the cut point. The complete
    final turn must still be evaluated at the first reduced newest-unit budget."""
    from core import fork_fit
    from core.checkpoint_fold_blocks import COLD_FOLD_OLD_TOOL_BUDGET_TOKENS
    from history.folds import checkpoint_fold_units

    call = {
        "id": "call-newest",
        "type": "function",
        "function": {"name": "inspect", "arguments": "a"},
    }
    source = _projection(
        _turn(
            1,
            (
                _message(1, "user", "INITIATING " * 400),
                _message(2, "assistant", "step one"),
                _message(3, "assistant", "step two"),
                _message(4, "assistant", "", tool_calls=(call,)),
                _message(
                    5,
                    "tool",
                    "R" * 120_000,
                    assistant_step_id="message-4",
                    tool_call_id="call-newest",
                    tool_name="inspect",
                ),
            ),
        )
    )
    units = checkpoint_fold_units(source)
    complete = fork_fit._candidate(
        source,
        units,
        skipped=0,
        project_tools=True,
        newest_tool_budget_tokens=COLD_FOLD_OLD_TOOL_BUDGET_TOKENS,
        protected_blocks=(),
        count_tokens=fork_fit._estimate_messages_tokens,
    )
    assert complete is not None and complete.input_tokens <= 2_600

    rendered = fork_fit.render_cold_fold_input(source, max_input_tokens=2_600)

    assert rendered.first_included_seq == 1
    assert rendered.messages == complete.messages
