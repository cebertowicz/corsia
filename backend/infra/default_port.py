"""The port CORSIA serves on when nothing overrides it.

Dependency-free so any backend module can import it. The standalone CLIs
(scripts/corsia_api.py), the shell entry point (scripts/corsia), the
installer (scripts/install.sh) and the desktop app's lookup of an earlier
source install (desktop/src-tauri/src/start_fresh.rs) are separate runtimes
with their own copy; tests/test_default_port.py holds all five equal.
"""

DEFAULT_BACKEND_PORT = 1956
