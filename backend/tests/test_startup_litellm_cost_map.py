"""The application loads LiteLLM's bundled cost map at startup."""

import os
import subprocess
import sys


def test_startup_does_not_fetch_litellm_cost_map(tmp_path):
    env = os.environ.copy()
    env.pop("LITELLM_LOCAL_MODEL_COST_MAP", None)
    env.update(
        CORSIA_WORKSPACE=str(tmp_path / "workspace"),
        CORSIA_STATE_DIR=str(tmp_path / "state"),
        CORSIA_TEST_MODE="1",
        CORSIA_ANALYTICS_DISABLE="1",
    )
    code = """
import httpx

def deny_fetch(*args, **kwargs):
    raise SystemExit('unexpected HTTP GET during startup')

httpx.get = deny_fetch
import main
import litellm
from litellm.litellm_core_utils.get_model_cost_map import get_model_cost_map_source_info
assert get_model_cost_map_source_info()['is_env_forced']
"""
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
