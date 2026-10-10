"""Harbor validates ``--ak`` kwargs against the agent's ``options_model``.

Harbor is not a backend dependency, so this skips where it is not installed.
Run it from the Harbor venv, with the repository root on ``PYTHONPATH``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("harbor")

# benchmarks/ sits at the repository root, outside backend/.
sys.path.append(str(Path(__file__).resolve().parents[2]))

from benchmarks.harbor.agents import MeteredCodexAgent  # noqa: E402

METER_KWARGS = {"call_cap": 200, "proxy_port": 18555}


def test_preflight_accepts_the_meter_kwargs():
    MeteredCodexAgent.preflight(METER_KWARGS, env={"OPENAI_API_KEY": "harbor-local"})


def test_meter_kwargs_are_options_not_codex_flags():
    options = MeteredCodexAgent.parse_options(
        {**METER_KWARGS, "proxy_bind": "127.0.0.1"}, env={"OPENAI_API_KEY": "harbor-local"},
    )

    assert (options.call_cap, options.proxy_port, options.proxy_bind) == (200, 18555, "127.0.0.1")
    assert options.proxy_host == "host.docker.internal"


def test_unknown_kwarg_is_still_rejected():
    with pytest.raises(ValueError, match="Unknown option 'proxy_prot'"):
        MeteredCodexAgent.preflight({"proxy_prot": 1}, env={"OPENAI_API_KEY": "harbor-local"})


def _agent(tmp_path, **kwargs):
    return MeteredCodexAgent(tmp_path, model_name="openai/test", **kwargs)


def test_codex_is_pinned_by_default(tmp_path):
    from benchmarks.harbor.agents import CODEX_VERSION_PIN

    assert CODEX_VERSION_PIN == "0.158.0"
    assert _agent(tmp_path).version() == CODEX_VERSION_PIN


def test_codex_pin_can_be_overridden_explicitly(tmp_path):
    assert _agent(tmp_path, version="0.159.0").version() == "0.159.0"


def test_installed_codex_version_is_recorded_beside_the_meter_receipt(tmp_path):
    import asyncio
    import json
    from types import SimpleNamespace

    from benchmarks.harbor.agents import CODEX_VERSION_RECEIPT

    class _Env:
        async def exec(self, command, **_kwargs):
            return SimpleNamespace(return_code=0, stdout="codex-cli 0.158.0\n")

    agent = _agent(tmp_path)
    asyncio.run(agent._record_installed_version(_Env()))

    assert json.loads((tmp_path / CODEX_VERSION_RECEIPT).read_text()) == {
        "pinned": "0.158.0", "installed": "0.158.0",
    }
