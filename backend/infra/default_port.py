"""The port CORSIA serves on when nothing overrides it.

Dependency-free so any backend module can import it. The standalone CLIs
(scripts/corsia_api.py), the shell entry point (scripts/corsia) and the
installer (scripts/install.sh) are separate runtimes with their own copy;
tests/test_default_port.py holds all four equal.
"""

DEFAULT_BACKEND_PORT = 1956
