"""Claude Code transcript lookup for one session id."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_HEAD_LINES = 50
_TAIL_BYTES = 65536
_PREVIEW_LEN = 120
_TITLE_LEN = 60
_HISTORY_LINES = 8000
_HISTORY_BYTES = 4 * 1024 * 1024

_JUNK_PREFIXES = ("<", "[", "Caveat:")


def resolve_claude_dir(claude_dir: str | None = None) -> str:
    """Return the explicit transcript root owned by one CC invocation."""
    if not claude_dir:
        from connections.account_policy import AccountAdmissionError

        raise AccountAdmissionError('account_not_configured')
    return str(Path(claude_dir).expanduser().resolve())


def _is_prompt(text: str | None) -> bool:
    """Return True for a typed prompt, excluding commands and injected frames."""
    t = (text or "").strip()
    return bool(t) and not t.startswith("/") and not t.startswith(_JUNK_PREFIXES)


def _take_title(d: dict, into: dict) -> None:
    """Fold a custom-title / ai-title / summary metadata line into ``into``."""
    t = d.get("type")
    if t == "custom-title" and d.get("customTitle"):
        into["custom"] = str(d["customTitle"]).strip() or into.get("custom")
    elif t == "ai-title" and d.get("aiTitle"):
        into["auto"] = str(d["aiTitle"]).strip() or into.get("auto")
    elif t == "summary" and d.get("summary"):
        into["auto"] = str(d["summary"]).strip() or into.get("auto")


def _head_scan(path: Path) -> dict:
    """Scan the first bounded lines for cwd, title, and first prompt."""
    cwd = None
    first_user = None
    titles: dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= _HEAD_LINES:
                    break
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None and d.get("cwd"):
                    cwd = d["cwd"]
                _take_title(d, titles)
                if first_user is None and d.get("type") == "user":
                    content = (d.get("message") or {}).get("content")
                    if isinstance(content, str) and _is_prompt(content):
                        first_user = content.strip()
    except OSError as e:
        log.debug("cc_sessions: head scan failed for %s: %s", path, e)
    return {"cwd": cwd, "first_user": first_user, "titles": titles}


def _tail_scan(path: Path) -> dict:
    """Scan the bounded tail for title lines and older last-prompt text."""
    titles: dict = {}
    last_prompt = None
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - _TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return {"titles": titles, "last_prompt": None}
    for line in tail.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        _take_title(d, titles)
        if d.get("type") == "last-prompt" and d.get("lastPrompt"):
            text = str(d["lastPrompt"]).strip()
            if _is_prompt(text):
                last_prompt = text
    return {"titles": titles, "last_prompt": last_prompt}


def _history_index(claude_dir: str) -> dict[str, dict]:
    """Build a bounded per-session prompt/title index from ``history.jsonl``."""
    path = Path(claude_dir) / "history.jsonl"
    idx: dict[str, dict] = {}
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - _HISTORY_BYTES))
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return idx
    for line in text.splitlines()[-_HISTORY_LINES:]:
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        sid = d.get("sessionId")
        display = str(d.get("display") or "").strip()
        if not sid or not display:
            continue
        ent = idx.setdefault(
            sid, {"renamed": None, "first_prompt": None, "last_prompt": None})
        if display.startswith("/rename "):
            ent["renamed"] = display[len("/rename "):].strip() or ent["renamed"]
        elif _is_prompt(display):
            if ent["first_prompt"] is None:
                ent["first_prompt"] = display
            ent["last_prompt"] = display
    return idx


def _clip(text: str | None, limit: int = _PREVIEW_LEN) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _session_meta(p: Path, hist: dict[str, dict]) -> dict:
    """Build one discovery row from bounded file scans and the history index."""
    head = _head_scan(p)
    tail = _tail_scan(p)
    h = hist.get(p.stem, {})
    first_prompt = h.get("first_prompt") or head["first_user"]
    title = (
        h.get("renamed")
        or tail["titles"].get("custom") or head["titles"].get("custom")
        or tail["titles"].get("auto") or head["titles"].get("auto")
        or first_prompt
    )
    preview = (_clip(h.get("last_prompt")) or _clip(tail["last_prompt"])
               or _clip(first_prompt))
    return {
        "session_id": p.stem,
        "path": str(p),
        "cwd": head["cwd"],
        "title": _clip(title, _TITLE_LEN),
        "preview": preview,
        "first_prompt": _clip(first_prompt),
    }


def cc_session_path(session_id: str, *, claude_dir: str) -> Path | None:
    """The transcript of one CC session under exactly this Claude root, if present."""
    projects = Path(resolve_claude_dir(claude_dir)) / "projects"
    if not projects.is_dir():
        return None
    return next(projects.glob(f"*/{session_id}.jsonl"), None)


def find_cc_session(thread_id: str, *, claude_dir: str) -> dict | None:
    """Locate one CC session by id across all projects of one Claude root."""
    path = cc_session_path(thread_id, claude_dir=claude_dir)
    if path is None:
        return None
    return _session_meta(path, _history_index(resolve_claude_dir(claude_dir)))


def require_resume_in_root(resume_session_id: str | None, transcript_path: str | None,
                           claude_dir: str | None) -> None:
    """Refuse a rebuilt transcript outside the Claude root its launch resumes from.

    Claude would report the session missing and end the turn with no cause;
    the named refusal keeps the input and says why.
    """
    if not (resume_session_id and transcript_path):
        return
    found = cc_session_path(resume_session_id, claude_dir=resolve_claude_dir(claude_dir))
    if found is None or found.resolve() != Path(transcript_path).resolve():
        from connections.account_policy import AccountAdmissionError

        raise AccountAdmissionError('account_session_home_mismatch')
