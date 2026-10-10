"""Small path helpers shared by reviewer and PRD mapping."""

from __future__ import annotations


def extract_changed_files_from_diff(diff_text: str) -> list[str]:
    """Extract changed file paths from a unified diff."""
    paths: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            paths.add(line[6:])
        elif line.startswith("--- a/") and line != "--- a//dev/null":
            path = line[6:]
            if path != "/dev/null":
                paths.add(path)
    return sorted(paths)


def path_matches(file_path: str, pattern: str) -> bool:
    """Check if a file path matches a PRD path-map pattern."""
    if pattern.startswith("*/"):
        suffix = pattern[2:]
        return f"/{suffix}" in f"/{file_path}"

    if pattern.startswith("*."):
        ext = pattern[1:]
        return file_path.endswith(ext)

    if pattern.endswith("/"):
        return file_path.startswith(pattern) or file_path == pattern.rstrip("/")

    return file_path == pattern
