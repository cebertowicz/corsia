"""Filesystem-based searchable agent memory."""

from __future__ import annotations

import re
import subprocess
import uuid
from infra.time import utc_now_iso
from pathlib import Path
from typing import Any

from loguru import logger

from infra.config import WORKSPACE

VALID_CATEGORIES = {"facts", "entities", "decisions", "preferences"}
KB_ROOT = WORKSPACE / "memory" / "kb"


def _kb_dir(category: str) -> Path:
    """Return the directory for a category, creating it if needed."""
    d = KB_ROOT / category
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slugify(text: str) -> str:
    """Convert a title to a filesystem-safe slug."""
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "-", slug)
    slug = slug.strip("-")
    return slug[:80] or "entry"


def _normalize_frontmatter_meta(meta: dict[str, Any]) -> dict[str, Any]:
    for key in ("created_at", "updated_at"):
        if key in meta and not isinstance(meta[key], str):
            meta[key] = str(meta[key])
    tags = meta.get("tags", [])
    if isinstance(tags, list):
        meta["tags"] = [str(tag) for tag in tags]
    elif tags:
        meta["tags"] = [str(tags)]
    else:
        meta["tags"] = []
    return meta


def _parse_frontmatter_fallback(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        return {}, content
    marker = "\n---\n"
    end = content.find(marker, 4)
    if end == -1:
        return {}, content
    meta: dict[str, Any] = {}
    for line in content[4:end].splitlines():
        if ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip()
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [
                part.strip().strip("\"'")
                for part in value[1:-1].split(",")
                if part.strip()
            ]
        else:
            meta[key] = value.strip("\"'")
    body = content[end + len(marker):]
    return _normalize_frontmatter_meta(meta), body.strip()


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from a knowledge entry."""
    try:
        import frontmatter
        post = frontmatter.loads(content)
        return _normalize_frontmatter_meta(dict(post.metadata)), post.content.strip()
    except ModuleNotFoundError:
        return _parse_frontmatter_fallback(content)
    except Exception:
        return {}, content


def write_entry(
    title: str,
    body: str,
    category: str,
    tags: list[str] | None = None,
) -> dict[str, str]:
    """Write a knowledge entry to the filesystem."""
    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}")

    tags = [str(tag) for tag in (tags or [])]
    now = utc_now_iso()
    entry_id = str(uuid.uuid4())[:8]
    slug = _slugify(title)
    category_dir = _kb_dir(category)
    file_path = category_dir / f"{slug}.md"

    action = "created"
    if file_path.exists():
        existing_content = file_path.read_text(encoding="utf-8")
        existing_meta, _ = _parse_frontmatter(existing_content)
        entry_id = existing_meta.get("id", entry_id)
        created_at = existing_meta.get("created_at", now)
        action = "updated"
    else:
        created_at = now

    tag_str = ", ".join(tags) if tags else ""
    frontmatter = (
        f"---\n"
        f"id: {entry_id}\n"
        f"category: {category}\n"
        f"tags: [{tag_str}]\n"
        f"created_at: {created_at}\n"
        f"updated_at: {now}\n"
        f"---\n\n"
    )

    file_path.write_text(frontmatter + f"# {title}\n\n{body}\n", encoding="utf-8")
    logger.debug("Knowledge entry {} ({}): {}", action, category, file_path.name)

    return {
        "id": entry_id,
        "path": str(file_path),
        "category": category,
        "action": action,
    }


def search_entries(
    query: str,
    category: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search knowledge entries by keyword."""
    search_root = KB_ROOT / category if category else KB_ROOT
    if not search_root.exists():
        return []

    results = _search_ripgrep(query, search_root, limit)
    if results is None:
        results = _search_python(query, search_root, limit)

    return results


def _search_ripgrep(query: str, root: Path, limit: int) -> list[dict[str, Any]] | None:
    """Search using ripgrep. Returns None if rg not available."""
    try:
        proc = subprocess.run(
            ["rg", "--ignore-case", "--with-filename", "--line-number",
             "--max-count=5", "--glob=*.md", query, str(root)],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode not in (0, 1):  # 1 = no matches (not an error)
            return None

        file_matches: dict[str, list[str]] = {}
        for line in proc.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) >= 3:
                filepath, _lineno, text = parts[0], parts[1], parts[2]
                stripped = text.strip()
                if stripped in ("---",) or re.match(r"^(id|category|tags|created_at|updated_at):", stripped):
                    continue
                file_matches.setdefault(filepath, []).append(stripped)

        results = []
        for filepath, snippets in list(file_matches.items())[:limit]:
            entry = _load_entry_meta(Path(filepath))
            if entry:
                clean = [s for s in snippets if s and not s.startswith("#")]
                entry["snippet"] = " … ".join(clean[:2]) if clean else ""
                results.append(entry)

        return results

    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _search_python(query: str, root: Path, limit: int) -> list[dict[str, Any]]:
    """Pure-Python fallback search."""
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []

    for md_file in sorted(root.rglob("*.md")):
        if md_file.name == "README.md":
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
        except OSError:
            continue

        if not pattern.search(content):
            continue

        entry = _load_entry_meta(md_file)
        if entry:
            m = pattern.search(content)
            if m:
                start = max(0, m.start() - 60)
                end = min(len(content), m.end() + 60)
                entry["snippet"] = "…" + content[start:end].strip() + "…"
            results.append(entry)
            if len(results) >= limit:
                break

    return results


def _load_entry_meta(file_path: Path) -> dict[str, Any] | None:
    """Load metadata from a knowledge entry file."""
    try:
        content = file_path.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(content)

        title_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else file_path.stem

        category = file_path.parent.name
        if category not in VALID_CATEGORIES:
            category = meta.get("category", "facts")

        return {
            "id": meta.get("id", ""),
            "title": title,
            "category": category,
            "tags": meta.get("tags", []),
            "path": str(file_path),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "snippet": "",
        }
    except (OSError, UnicodeDecodeError) as e:
        from loguru import logger
        logger.warning(f"KB entry load failed for {file_path}: {e}")
        return None


def list_entries(category: str | None = None) -> list[dict[str, Any]]:
    """List all knowledge entries, optionally filtered by category."""
    search_root = KB_ROOT / category if category else KB_ROOT
    if not search_root.exists():
        return []

    results = []
    for md_file in sorted(search_root.rglob("*.md")):
        if md_file.name == "README.md":
            continue
        entry = _load_entry_meta(md_file)
        if entry:
            results.append(entry)

    return results


def get_recent_entries(limit: int = 5) -> list[dict[str, Any]]:
    """Get the most recently updated entries across all categories, with body content."""
    all_entries = list_entries()
    all_entries.sort(key=lambda e: e.get("updated_at", ""), reverse=True)
    recent = all_entries[:limit]

    for entry in recent:
        try:
            content = Path(entry["path"]).read_text(encoding="utf-8")
            _, body = _parse_frontmatter(content)
            body_lines = body.splitlines()
            if body_lines and body_lines[0].startswith("# "):
                body_lines = body_lines[1:]
            entry["body"] = "\n".join(body_lines).strip()
        except OSError:
            entry["body"] = ""

    return recent
