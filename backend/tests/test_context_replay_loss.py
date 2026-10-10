"""In-band notices for actual replay loss in provider context."""

from __future__ import annotations

from types import SimpleNamespace


def _tail(start_exclusive, *, retention_stub_seqs=(), missing_result_stub_seqs=()):
    return SimpleNamespace(
        start_exclusive=start_exclusive,
        sealed_boundary=SimpleNamespace(seq=40),
        retention_stub_seqs=retention_stub_seqs,
        missing_result_stub_seqs=missing_result_stub_seqs,
    )


def _selection(*, mode="checkpoint_tail", start_seq=10, retention=(), missing=()):
    from core.context_replay import ReplaySelection
    from history.types import HistoryBoundary

    checkpoint = HistoryBoundary("t", 1, 10, "message-10")
    start = None if start_seq is None else HistoryBoundary(
        "t", 1, start_seq, f"message-{start_seq}"
    )
    return ReplaySelection(
        mode=mode,
        history_epoch=1,
        sealed_boundary=HistoryBoundary("t", 1, 40, "message-40"),
        tail=_tail(
            start,
            retention_stub_seqs=retention,
            missing_result_stub_seqs=missing,
        ),
        checkpoint_boundary=checkpoint if mode == "checkpoint_tail" else None,
        checkpoint_json="{}" if mode == "checkpoint_tail" else None,
    )


def test_replay_declares_only_lost_tool_results_after_checkpoint_boundary():
    from core.context_replay import history_loss_notice

    assert history_loss_notice(_selection(missing=(3, 7))) == ""
    notice = history_loss_notice(_selection(missing=(21,)))
    assert "NEVER RECORDED" in notice
    assert "Verify current state" in notice


def test_truncated_replay_declares_its_gap():
    from core.context_replay import history_loss_notice

    assert history_loss_notice(_selection()) == ""
    notice = history_loss_notice(_selection(start_seq=25))
    assert "position 10 and position 25" in notice
    assert "DROPPED" in notice
    assert "not summarized anywhere" in notice
    assert history_loss_notice(_selection(mode="exact", start_seq=None)) == ""


def test_retention_holed_replay_declares_only_uncovered_payloads():
    from core.context_replay import history_loss_notice

    assert history_loss_notice(_selection(retention=(3, 7))) == ""
    notice = history_loss_notice(
        _selection(mode="exact", start_seq=None, retention=(3, 7, 99))
    )
    assert "2 tool result(s)" in notice
    assert "discarded by retention" in notice
    assert "The calls did run" in notice
