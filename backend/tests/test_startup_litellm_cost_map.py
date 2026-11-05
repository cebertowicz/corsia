"""The application loads LiteLLM's bundled cost map at startup."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("operator_value, offline", [(None, False), ("true", True), ("True", True)])
def test_startup_does_not_fetch_litellm_cost_map(tmp_path, operator_value, offline):
    env = os.environ.copy()
    env.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    if operator_value is not None:
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = operator_value
    env.update(
        CORSIA_WORKSPACE=str(tmp_path / "workspace"),
        CORSIA_STATE_DIR=str(tmp_path / "state"),
        CORSIA_TEST_MODE="1",
        CORSIA_ANALYTICS_DISABLE="1",
    )
    code = f"""
import httpx

def deny_fetch(*args, **kwargs):
    raise SystemExit('unexpected HTTP GET during startup')

httpx.get = deny_fetch
import main
import litellm
from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info
assert get_model_cost_map_source_info()['is_env_forced']
# Only the operator's own setting keeps CORSIA's post-startup refresh offline.
assert main.app.state.litellm_cost_map_offline is {offline}
"""
    relaunch = (
        "import os, subprocess, sys\n" + code
        # A relaunch from this backend's environment, as the in-app restart does.
        + "subprocess.run([sys.executable, '-c', CODE], env=dict(os.environ), check=True)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", f"CODE = {code!r}\n" + relaunch],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
