"""Reviewer calibration output belongs to operational state, not workspace content."""

from __future__ import annotations


def test_calibration_data_uses_the_state_root(
    monkeypatch,
    tmp_path,
) -> None:
    from infra import config
    from reviewer.reviewer_backends import write_calibration_data

    state_root = tmp_path / "state"
    monkeypatch.setattr(config, "STATE_ROOT", state_root)

    write_calibration_data(
        42,
        '{"summary":"cloud"}',
        '{"summary":"local"}',
        cc_label="Codex reviewer",
    )

    calibration_dir = state_root / "reviewer" / "calibration" / "42"
    assert (calibration_dir / "cc.md").read_text(encoding="utf-8") == (
        '# Codex reviewer Review -- PR #42\n\n'
        '```json\n{"summary":"cloud"}\n```\n'
    )
    assert (calibration_dir / "qwen-27b.md").is_file()
    assert (calibration_dir / "diff.md").is_file()
