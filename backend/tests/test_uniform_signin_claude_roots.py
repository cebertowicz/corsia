"""Claude processes and transcript lookups require an explicit account home."""

from types import SimpleNamespace

import pytest


def test_transcript_root_refuses_missing_home(monkeypatch, tmp_path):
    from connections.account_policy import AccountAdmissionError
    from subagents.cc_sessions import resolve_claude_dir

    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'ambient'))
    with pytest.raises(AccountAdmissionError):
        resolve_claude_dir(None)


def test_process_home_refuses_missing_home(monkeypatch, tmp_path):
    from connections.account_policy import AccountAdmissionError
    from subagents.cc_env import apply_claude_config_dir

    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'ambient'))
    with pytest.raises(AccountAdmissionError):
        apply_claude_config_dir({}, None)


def test_warm_capture_does_not_look_up_an_ambient_transcript(monkeypatch):
    from core.fork_cc import _identity_from_live
    from subagents import cc_sessions

    def unexpected_lookup(*args, **kwargs):
        raise AssertionError('missing account home must not search transcripts')

    monkeypatch.setattr(cc_sessions, 'find_cc_session', unexpected_lookup)
    invocation = SimpleNamespace(resume_session_id=None, session_id='synthetic')
    session = SimpleNamespace(invocation=lambda: invocation, claude_dir=None)
    assert _identity_from_live(SimpleNamespace(session=session, claude_dir=None)) is None


def test_process_home_is_explicit(tmp_path):
    from subagents.cc_env import apply_claude_config_dir

    home = str(tmp_path / 'account')
    env = {}
    apply_claude_config_dir(env, home)
    assert env['CLAUDE_CONFIG_DIR'] == home
