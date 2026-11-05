"""Real FIFO claims and renderer counts guard both subscription steer writes."""

import pytest

pytestmark = pytest.mark.usefixtures('signed_in_defaults')


@pytest.mark.asyncio
async def test_cc_only_the_last_held_reply_owner_writes_the_steer(isolated_db, monkeypatch):
    import asyncio
    from infra.database import get_db
    from subagents import cc_runtime_steering
    from test_clean_cc_boundary import _seed_boundary, _install_runtime, _steer_row

    with get_db() as db:
        current = _seed_boundary(db)
    writes = []

    async def feed(text, *, on_write_observed, on_accepted, input_context=None, input_id=None):
        writes.append(text)
        on_write_observed()
        on_accepted()

    live = _install_runtime(monkeypatch, cc_runtime_steering, current, feed)
    sibling = asyncio.create_task(asyncio.sleep(3600))
    live.admitted_tool_tasks.add(sibling)
    try:
        assert not live.tool_step_ids
        assert not await cc_runtime_steering.deliver_live_inbox_head("thread-1")
        with get_db() as db:
            assert tuple(_steer_row(db))[:3] == (None, "queued", None)
        assert writes == []
    finally:
        live.admitted_tool_tasks.discard(sibling)
        sibling.cancel()
    live.admitted_tool_tasks.clear()
    assert not await cc_runtime_steering.deliver_live_inbox_head("thread-1")
    assert writes == []
    live.admitted_tool_tasks.add(asyncio.current_task())
    assert await cc_runtime_steering.deliver_live_inbox_head("thread-1")
    assert writes == ["steer now"]


@pytest.mark.asyncio
async def test_native_prefix_budget_does_not_reserve_future_output():
    import asyncio
    from types import SimpleNamespace
    from core.turn_safe_boundary import claim_safe_boundary

    state = SimpleNamespace(
        fit_ledger=SimpleNamespace(effective_window_tokens=1000, rendered_input_tokens=900,
                                   total_tokens=1200),
        last_measured_call_usage=None, last_measured_rendered_input_tokens=None,
    )
    budgets = []

    def claim(remaining):
        budgets.append(remaining)

    await claim_safe_boundary(state, "thread", None, asyncio.Event(), claim)
    assert budgets == [100]


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "claude-code"])
async def test_oversized_batch_delivers_fitting_send_prefix_and_releases_suffix(isolated_db, monkeypatch, harness):
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from test_codex_inbox_steer import _seed_live_turn, _install_live, _clear_live
    from test_clean_cc_boundary import _seed_boundary, _install_runtime
    from subagents import codex_inbox_batch, cc_runtime_steering

    writes = []

    class Session:
        def peek_active_usage(self):
            return {"input_tokens": 32568, "model_context_window": 1000000}

        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            writes.append(text)
            on_write_observed()
            return True

        async def feed(self, text, *, on_write_observed, on_accepted, input_context=None, input_id=None):
            writes.append(text)
            on_write_observed()
            on_accepted()

    if harness == "codex":
        thread_id, first_id = "fitting-prefix", "first"
        live = _install_live(thread_id, _seed_live_turn(thread_id), Session())
        accept_thread_input(thread_id, role="user", content="first " + "word " * 100,
                            intent="apply_now", message_id=first_id)
        deliver = codex_inbox_batch.deliver_live_inbox_batch
    else:
        thread_id, first_id = "thread-1", "steer"
        with get_db() as db:
            current = _seed_boundary(db)
            db.execute("UPDATE messages SET content=? WHERE id='steer'", ("first " + "word " * 100,))
            db.commit()
        live = _install_runtime(monkeypatch, cc_runtime_steering, current, Session().feed)
        live.last_call_usage = {"input_tokens": 32568, "output_tokens": 0}
        live.session.tool_context.base = 32568
        live.session.tool_context.input_measured = True
        deliver = cc_runtime_steering.deliver_live_inbox_head
    live.context_window = 32768  # Valid checkpoint cap, with 200 tokens left.
    with get_db() as db:
        db.execute("INSERT INTO model_profiles(model_id,context_window,updated_at) "
                   "SELECT model,32768,'t' FROM threads WHERE id=? "
                   "ON CONFLICT(model_id) DO UPDATE SET context_window=32768", (thread_id,))
        db.commit()
    accept_thread_input(thread_id, role="user", content="second " + "word " * 100,
                        intent="apply_now", message_id="second")
    try:
        assert await deliver(thread_id) is True
        assert len(writes) == 1 and "first" in writes[0] and "second" not in writes[0]
        with get_db() as db:
            first = db.execute("SELECT delivery_state FROM messages WHERE id=?", (first_id,)).fetchone()
            tail = db.execute("SELECT delivery_state,active_turn_id,delivery_attempt_id,seq "
                              "FROM messages WHERE id='second'").fetchone()
        assert first[0] == "delivered"
        assert tuple(tail) == ("queued", None, None, None)
    finally:
        if harness == "codex":
            _clear_live(thread_id)


@pytest.mark.asyncio
async def test_native_boundary_selects_fitting_prefix_without_claiming_the_tail(isolated_db):
    from test_native_inbox_batch import _seed, _enqueue, _thread_row
    from ws.native_inbox_batch import native_safe_boundary_input
    from infra.database import get_db

    thread = "native-prefix-fit"
    identity = _seed(thread)
    _enqueue(thread, "first", "first " + "word " * 100)
    _enqueue(thread, "second", "second " + "word " * 100)
    candidate = await native_safe_boundary_input(
        thread_id=thread, thread_row=_thread_row(thread), owner_turn_id=identity.turn_id,
        max_input_tokens=200,
    )
    assert candidate is not None
    assert "first" in candidate.content and "second" not in candidate.content
    await candidate.before_provider_write()
    await candidate.settle_delivery()
    with get_db() as db:
        tail = db.execute("SELECT delivery_state,active_turn_id,delivery_attempt_id,seq "
                          "FROM messages WHERE id='second'").fetchone()
    assert tuple(tail) == ("queued", None, None, None)


@pytest.mark.asyncio
async def test_live_prefix_keeps_explicit_queue_with_its_admitting_send(isolated_db):
    from test_native_inbox_batch import _seed, _enqueue, _thread_row
    from ws.native_inbox_batch import native_safe_boundary_input
    from infra.database import get_db

    thread = "queued-prefix-fit"
    identity = _seed(thread)
    _enqueue(thread, "first", "first " + "word " * 100, intent="queue_after_turn")
    _enqueue(thread, "second", "second " + "word " * 100)
    kwargs = dict(thread_id=thread, thread_row=_thread_row(thread), owner_turn_id=identity.turn_id)
    assert await native_safe_boundary_input(**kwargs, max_input_tokens=200) is None
    with get_db() as db:
        rows = db.execute("SELECT active_turn_id,delivery_attempt_id,seq FROM messages "
                          "WHERE id IN ('first','second')").fetchall()
    assert [tuple(row) for row in rows] == [(None, None, None), (None, None, None)]
    candidate = await native_safe_boundary_input(**kwargs, max_input_tokens=500)
    assert candidate is not None
    assert "first" in candidate.content and "second" in candidate.content
    await candidate.before_provider_write()
    await candidate.settle_delivery()


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex", "claude-code"])
async def test_repeated_steers_do_not_reuse_the_same_capacity(isolated_db, monkeypatch, harness):
    from infra.database import get_db
    from infra.input_admission import accept_thread_input
    from test_codex_inbox_steer import _seed_live_turn, _install_live, _clear_live
    from test_clean_cc_boundary import _seed_boundary, _install_runtime
    from subagents import codex_inbox_batch, cc_runtime_steering

    writes = []

    class Session:
        def peek_active_usage(self):
            return {"input_tokens": 32568, "model_context_window": 1_000_000}

        async def steer(self, text, *, on_write_observed, on_drain_confirmed, input_context=None):
            writes.append(text)
            on_write_observed()
            self.tool_context.pending_inputs[input_context[0]] = input_context[1]
            return True

        async def feed(self, text, *, on_write_observed, on_accepted, input_context=None, input_id=None):
            writes.append(text)
            on_write_observed()
            live.session.tool_context.pending_inputs[input_context[0]] = input_context[1]
            on_accepted()

    if harness == "codex":
        thread_id = "repeated-capacity"
        identity = _seed_live_turn(thread_id)
        live = _install_live(thread_id, identity, Session())
        deliver = codex_inbox_batch.deliver_live_inbox_batch
    else:
        thread_id = "thread-1"
        with get_db() as db:
            current = _seed_boundary(db)
            # The seed's pending row is the first actual steer in this test.
            db.execute("UPDATE messages SET content=? WHERE id='steer'", ("word " * 100,))
            db.commit()
        live = _install_runtime(monkeypatch, cc_runtime_steering, current, Session().feed)
        live.last_call_usage = {"input_tokens": 32568, "output_tokens": 0}
        live.session.tool_context.base = 32568
        live.session.tool_context.input_measured = True
        deliver = cc_runtime_steering.deliver_live_inbox_head
    live.context_window = 32768  # Valid checkpoint cap, with 200 tokens left.
    with get_db() as db:
        db.execute("INSERT INTO model_profiles(model_id,context_window,updated_at) "
                   "SELECT model,32768,'t' FROM threads WHERE id=? "
                   "ON CONFLICT(model_id) DO UPDATE SET context_window=32768", (thread_id,))
        db.commit()
    try:
        if harness == "codex":
            accept_thread_input(thread_id, role="user", content="word " * 100,
                                intent="apply_now", message_id="first-steer")
        assert await deliver(thread_id) is True
        accept_thread_input(thread_id, role="user", content="word " * 100,
                            intent="apply_now", message_id="second-steer")
        assert await deliver(thread_id) is False
        assert len(writes) == 1
        with get_db() as db:
            row = db.execute(
                "SELECT delivery_state,active_turn_id,delivery_attempt_id,seq "
                "FROM messages WHERE id='second-steer'",
            ).fetchone()
        assert tuple(row) == ("queued", None, None, None)
        # A later boundary with room can claim these same IDs; no resend needed.
        with get_db() as db:
            db.execute("UPDATE model_profiles SET context_window=65536 WHERE model_id="
                       "(SELECT model FROM threads WHERE id=?)", (thread_id,))
            db.commit()
        assert await deliver(thread_id) is True
        assert len(writes) == 2
    finally:
        if harness == "codex":
            _clear_live(thread_id)
