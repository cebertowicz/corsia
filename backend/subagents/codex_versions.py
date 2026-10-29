"""The Codex versions read-only agents may run on, and the one list CI installs from.

Dependency-free so CI can print the list before backend packages are installed:
``python backend/subagents/codex_versions.py`` prints the versions oldest first.
"""
from __future__ import annotations

# CI captures each admitted version on cold start and process resume.
CAPTURED_CODEX_VERSIONS = frozenset({
    "codex-cli 0.162.1", "codex-cli 0.163.0", "codex-cli 0.164.0", "codex-cli 0.165.0",
})


def captured_versions() -> list[str]:
    """The bare version numbers, oldest first."""
    return sorted(
        (value.removeprefix("codex-cli ") for value in CAPTURED_CODEX_VERSIONS),
        key=lambda value: tuple(int(part) for part in value.split(".")),
    )


if __name__ == "__main__":
    print(" ".join(captured_versions()))
