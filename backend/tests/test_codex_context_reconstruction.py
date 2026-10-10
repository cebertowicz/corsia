from __future__ import annotations


def _boundary(seq: int, message_id: str):
    from history.types import HistoryBoundary

    return HistoryBoundary(
        thread_id="sess-ctx",
        history_epoch=7,
        seq=seq,
        message_id=message_id,
    )


def _message(
    message_id: str,
    seq: int,
    role: str,
    content: str,
    **kwargs,
):
    from history.types import PortableMessage

    return PortableMessage(
        message_id=message_id,
        seq=seq,
        turn_id=kwargs.pop("turn_id", "turn-1"),
        role=role,
        content=content,
        created_at=f"time-{seq}",
        **kwargs,
    )


def _projection(*turn_messages, format_version: int = 1, start_exclusive=None):
    from history.types import (
        PortableHistoryProjection,
        PortableTurn,
    )

    turns = []
    for index, messages in enumerate(turn_messages, start=1):
        boundary = _boundary(messages[-1].seq, messages[-1].message_id)
        turns.append(PortableTurn(
            turn_id=f"turn-{index}",
            status="completed",
            messages=tuple(messages),
            boundary=boundary,
        ))
    return PortableHistoryProjection(
        format_version=format_version,
        thread_id="sess-ctx",
        history_epoch=7,
        start_exclusive=start_exclusive,
        sealed_boundary=turns[-1].boundary if turns else start_exclusive,
        turns=tuple(turns),
        full_raw_replay_available=True,
    )


def test_codex_history_items_keep_repeated_identical_user_turns() -> None:
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )

    projection = _projection(
        (
            _message("u1", 1, "user", "same request", turn_id="turn-1"),
            _message("a1", 2, "assistant", "first answer", turn_id="turn-1"),
        ),
        (
            _message("u2", 3, "user", "same request", turn_id="turn-2"),
            _message("a2", 4, "assistant", "second answer", turn_id="turn-2"),
        ),
    )

    reconstruction = build_codex_context_reconstruction(
        thread_id="sess-ctx",
        user_text="same request",
        working_dir="/workspace",
        project_id="proj-1",
        history_projection=projection,
    )

    assert [
        item["content"][0]["text"]
        for item in reconstruction.history_items
        if item["role"] == "user"
    ] == ["same request", "same request"]
    assert reconstruction.snapshot.frozen_prompt_material["history"] == list(
        reconstruction.history_items
    )


def test_codex_adapter_is_thin_ordered_and_explicitly_lossy() -> None:
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
        codex_inject_items,
    )

    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "Read", "arguments": '{"path":"a"}'},
    }
    projection = _projection((
        _message(
            "u1",
            1,
            "user",
            "inspect",
            artifact_references=({"artifact_id": "artifact-1", "path": "a"},),
        ),
        _message(
            "a1",
            2,
            "assistant",
            "checking",
            tool_calls=(call,),
            reasoning_text="private chain",
            reasoning_provenance={
                "source_harness": "native",
                "source_provider": "omlx",
                "representation_kind": "reasoning_content",
            },
        ),
        _message(
            "r1",
            3,
            "tool",
            "file contents",
            assistant_step_id="a1",
            tool_call_id="call-1",
            tool_name="Read",
        ),
    ))

    items = codex_inject_items(projection)

    assert [item["role"] for item in items] == ["user", "assistant", "user"]
    assert "[artifact references]" in items[0]["content"][0]["text"]
    assert "artifact-1" in items[0]["content"][0]["text"]
    assert "[assistant tool calls]" in items[1]["content"][0]["text"]
    assert "call-1" in items[1]["content"][0]["text"]
    assert items[2]["content"][0]["text"] == (
        "[tool result: read_file; call_id=call-1]\nfile contents"
    )
    # Codex's proven inject-items contract has no typed reasoning item. It is
    # deliberately omitted rather than being reclassified as assistant prose.
    assert "private chain" not in str(items)

    reconstruction = build_codex_context_reconstruction(
        thread_id="sess-ctx",
        user_text="current request",
        working_dir="/workspace",
        project_id="proj-1",
        history_projection=projection,
    )
    counted_history = reconstruction.snapshot.frozen_prompt_material["history"]
    assert counted_history == list(items)
    assert "private chain" not in str(counted_history)
    portable_source = reconstruction.snapshot.frozen_prompt_material[
        "provider_payload"
    ]["portable_history"]
    assert portable_source["format_version"] == projection.format_version
    assert portable_source["sealed_boundary"] == {
        "thread_id": "sess-ctx",
        "history_epoch": 7,
        "seq": 3,
        "message_id": "r1",
    }


def test_codex_tool_result_labels_preserve_parallel_same_name_call_ids() -> None:
    from subagents.codex_context_reconstruction import codex_inject_items

    calls = tuple({
        "id": call_id,
        "type": "function",
        "function": {"name": "Read", "arguments": "{}"},
    } for call_id in ("call-a", "call-b"))
    projection = _projection((
        _message("u1", 1, "user", "inspect both"),
        _message("a1", 2, "assistant", "checking", tool_calls=calls),
        _message(
            "r1", 3, "tool", "first", assistant_step_id="a1",
            tool_call_id="call-a", tool_name="Read",
        ),
        _message(
            "r2", 4, "tool", "second", assistant_step_id="a1",
            tool_call_id="call-b", tool_name="Read",
        ),
    ))

    items = codex_inject_items(projection)
    result_texts = [item["content"][0]["text"] for item in items[-2:]]

    assert result_texts == [
        "[tool result: read_file; call_id=call-a]\nfirst",
        "[tool result: read_file; call_id=call-b]\nsecond",
    ]


def test_reconstruction_preserves_portable_source_version_and_boundaries() -> None:
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )

    start = _boundary(2, "a0")
    projection = _projection(
        (
            _message("u1", 3, "user", "next", turn_id="turn-1"),
            _message("a1", 4, "assistant", "done", turn_id="turn-1"),
        ),
        format_version=9,
        start_exclusive=start,
    )

    reconstruction = build_codex_context_reconstruction(
        thread_id="sess-ctx",
        user_text="current",
        working_dir="/workspace",
        project_id="proj-1",
        history_projection=projection,
    )

    assert reconstruction.history_format_version == 9
    assert reconstruction.history_start_exclusive == start
    assert reconstruction.history_sealed_boundary == projection.sealed_boundary
    assert reconstruction.history_full_raw_replay_available is True


def test_checkpoint_is_injected_before_exact_tail_without_task_duplication() -> None:
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )

    projection = _projection(
        (
            _message("u-tail", 3, "user", "tail request", turn_id="turn-tail"),
            _message(
                "a-tail",
                4,
                "assistant",
                "tail answer",
                turn_id="turn-tail",
            ),
        ),
        start_exclusive=_boundary(2, "checkpoint-boundary"),
    )
    checkpoint_text = (
        "[CORSIA checkpoint]\n"
        '{"objective":"continue from the installed boundary"}'
    )

    reconstruction = build_codex_context_reconstruction(
        thread_id="sess-ctx",
        user_text="current request",
        working_dir="/workspace",
        project_id="proj-1",
        history_projection=projection,
        checkpoint_text=checkpoint_text,
    )

    assert [
        item["content"][0]["text"] for item in reconstruction.history_items
    ] == [checkpoint_text, "tail request", "tail answer"]
    assert reconstruction.history_items[0]["role"] == "user"
    assert checkpoint_text not in reconstruction.task_text
    assert reconstruction.snapshot.frozen_prompt_material["history"] == list(
        reconstruction.history_items
    )
    checkpoint_source = next(
        source
        for source in reconstruction.snapshot.source_inventory
        if source.source_id == "history:portable-checkpoint"
    )
    assert checkpoint_source.prompt_content is None


def test_prepared_destination_freezes_the_actual_cold_codex_payload() -> None:
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )
    from subagents.codex_destination import build_prepared_codex_destination

    projection = _projection((
        _message("u1", 1, "user", "prior request"),
        _message("a1", 2, "assistant", "prior answer"),
    ))
    reconstruction = build_codex_context_reconstruction(
        thread_id="sess-ctx",
        user_text="current request",
        working_dir="/workspace",
        project_id="proj-1",
        history_projection=projection,
    )

    destination = build_prepared_codex_destination(
        thread_id="sess-ctx",
        target_catalog_id="codex/gpt-5-codex",
        current_user_text="current request",
        reconstruction=reconstruction,
        delivery_mode="cold_start",
        handle_fingerprint=None,
        tool_registry=object(),
        dynamic_tools=({
            "name": "sentinel_tool",
            "description": "Frozen at preflight",
            "inputSchema": {"type": "object", "properties": {}},
        },),
    )

    assert destination.provider_text == reconstruction.task_text
    assert destination.history_items is reconstruction.history_items
    assert destination.accounting_payload == {
        "task_text": reconstruction.task_text,
        "turn_text": None,
        "history_items": list(reconstruction.history_items),
        "dynamic_tools": [{
            "name": "sentinel_tool",
            "description": "Frozen at preflight",
            "inputSchema": {"type": "object", "properties": {}},
        }],
    }
    assert destination.input_tokens > 0


def test_prepared_live_destination_does_not_count_unresent_tool_schemas() -> None:
    from subagents.codex_context_reconstruction import (
        build_codex_context_reconstruction,
    )
    from subagents.codex_destination import build_prepared_codex_destination
    from subagents.codex_runtime_state import CodexHandleFingerprint

    reconstruction = build_codex_context_reconstruction(
        thread_id="sess-ctx",
        user_text="current request",
        working_dir="/workspace",
        project_id="proj-1",
        history_projection=_projection(),
    )
    fingerprint = CodexHandleFingerprint(
        codex_thread_id="provider-thread",
        live_identity=1,
        session_identity=2,
    )
    frozen_registry = object()
    frozen_tools = ({
        "name": "sentinel_tool",
        "description": "Already advertised when the handle started",
        "inputSchema": {"type": "object", "properties": {}},
    },)

    destination = build_prepared_codex_destination(
        thread_id="sess-ctx",
        target_catalog_id="codex/gpt-5-codex",
        current_user_text="current request",
        reconstruction=reconstruction,
        delivery_mode="provider_resume",
        handle_fingerprint=fingerprint,
        tool_registry=frozen_registry,
        dynamic_tools=frozen_tools,
    )
    without_tools = build_prepared_codex_destination(
        thread_id="sess-ctx",
        target_catalog_id="codex/gpt-5-codex",
        current_user_text="current request",
        reconstruction=reconstruction,
        delivery_mode="provider_resume",
        handle_fingerprint=fingerprint,
        tool_registry=frozen_registry,
        dynamic_tools=(),
    )

    assert destination.dynamic_tools is frozen_tools
    assert destination.accounting_payload["dynamic_tools"] == []
    assert destination.input_tokens == without_tools.input_tokens


def test_codex_historical_reconstruction_never_replays_typed_images() -> None:
    from subagents.codex_context_reconstruction import codex_inject_items

    projection = _projection((
        _message(
            "u-image",
            1,
            "user",
            "historical image request",
            artifact_references=({
                "artifact_id": "upload-image",
                "mime_type": "image/png",
                "sha256": "a" * 64,
            },),
        ),
        _message("a-image", 2, "assistant", "historical answer"),
    ))

    items = codex_inject_items(projection)
    content_blocks = [block for item in items for block in item["content"]]

    assert [block["type"] for block in content_blocks] == [
        "input_text",
        "output_text",
    ]
    assert all("url" not in block for block in content_blocks)
