"""CI-only: the triage stage executes in real Safehouse through the GitHub broker."""
import json
import subprocess
from pathlib import Path

import pytest

from tests.rule_worker_helpers import fenced, run_scripts, settle_all, starts, world  # noqa: F401
from tests.test_github_screen_path import screen  # noqa: F401
from tests.test_github_outside_triage import route
from tests.test_script_sandbox_real import sandbox, short_system_temp  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_safehouse_triage_stages_through_broker(screen, sandbox, monkeypatch):
    from automation import script_github_channel, script_runner, script_sandbox
    from connections.github_broker import GithubReadBroker
    from events.connectors.github_screen.workflow import workflow
    from infra import config
    from tools import sandbox_config

    paths = []
    def transport(args, *, timeout):
        paths.append(args[-1])
        return subprocess.CompletedProcess(args, 0, json.dumps({'id': 42, 'body': 'untrusted item'}), '')

    choices = workflow(screen['project'], 'github-outside-triage').corsia['workflow']['result']['properties']['payload']['properties']['next']['enum']
    screen['reply'].write_text(fenced({'payload': {'next': choices[0], 'summary': 'report', 'findings': [], 'refs': []}, 'injection_signs': []}))
    monkeypatch.setattr(script_github_channel, 'default_broker', lambda: GithubReadBroker(transport))
    monkeypatch.setattr(script_runner, 'run_script', script_sandbox.run_script)
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RW', [sandbox.parent, Path(config.WORKSPACE).resolve()])
    await route('issue_comment', 'created')
    await run_scripts()
    await settle_all()
    assert [row[0] for row in starts(screen['key'] + '_triage_start')] == ['returned']
    assert paths == ['repos/o/r/issues/comments/42']
    assert not list((screen['folder'] / 'staging').glob('triage-*'))
