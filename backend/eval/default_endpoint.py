"""Default eval endpoint of a local CORSIA backend."""

from infra.default_port import DEFAULT_BACKEND_PORT

DEFAULT_EVAL_BASE = f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}"
DEFAULT_EVAL_ENDPOINT = f"{DEFAULT_EVAL_BASE}/api/eval/turn"
