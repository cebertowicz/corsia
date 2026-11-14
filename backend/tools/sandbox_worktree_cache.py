"""Reuse reciprocal worktree proofs only for unchanged canonical paths."""

import stat
from pathlib import Path


# Successful proofs only; unusual path shapes always use the caller's verifier.
_worktree_proofs: dict[tuple[Path, Path], tuple[Path, tuple]] = {}


def _proof_signature(repo: Path, entry: Path, gitdir: Path) -> tuple | None:
    paths = (
        (repo / ".worktrees", True), (entry, True),
        (repo / ".git", True), (repo / ".git" / "worktrees", True),
        (gitdir, True), (entry / ".git", False), (gitdir / "gitdir", False),
    )
    signatures = []
    try:
        for path, directory in paths:
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                return None
            identity = (info.st_dev, info.st_ino, info.st_mode)
            # Sibling additions change directory timestamps, not their identity.
            signatures.append(identity if directory else (
                *identity, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            ))
    except OSError:
        return None
    return tuple(signatures)


def cached_list_worktree(entry: Path, repo: Path, cache: dict, verify) -> Path | None:
    """Reuse only a canonical proof with unchanged non-symlink components."""
    key = (repo, entry)
    previous = _worktree_proofs.get(key)
    if previous is not None:
        # As with the underlying verifier, a mutation racing this call can occur
        # after its check; completed mutations are checked on the next call.
        gitdir, signature = previous
        if _proof_signature(repo, entry, gitdir) == signature:
            return entry
        _worktree_proofs.pop(key, None)

    verified = verify(entry, repo, cache=cache)
    if verified is None or verified[0] != entry:
        return verified[0] if verified else None
    gitdir = verified[1]
    if gitdir.parent != repo / ".git" / "worktrees":
        return entry
    before = _proof_signature(repo, entry, gitdir)
    if before is None:
        return entry
    try:
        marker = (entry / ".git").read_text(encoding="utf-8").strip()
        reciprocal = (gitdir / "gitdir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return entry
    if (marker == f"gitdir: {gitdir}" and reciprocal == str(entry / ".git")
            and _proof_signature(repo, entry, gitdir) == before):
        _worktree_proofs[key] = (gitdir, before)
    return entry


def forget_worktree(repo: Path, entry: Path) -> None:
    _worktree_proofs.pop((repo, entry), None)


def list_worktrees(repo_root: Path, verify, root_for_path) -> list[Path]:
    parent = repo_root.expanduser().resolve(strict=False) / ".worktrees"
    if not parent.is_dir():
        return []
    worktrees = []
    cache: dict = {}
    repo = parent.parent
    seen = set()
    plain_parent = not parent.is_symlink()
    try:
        for entry in parent.iterdir():
            resolved = (entry if plain_parent and not entry.is_symlink()
                        else entry.resolve(strict=False))
            cache[("sandbox-resolved-path", str(entry))] = resolved
            seen.add((repo, entry))
            if resolved == entry and plain_parent:
                root = cached_list_worktree(entry, repo, cache, verify)
            else:
                forget_worktree(repo, entry)
                root = root_for_path(entry, repo_root, cache=cache)
            if root == resolved:
                worktrees.append(resolved)
    except OSError:
        return []
    for key in list(_worktree_proofs):
        if key[0] == repo and key not in seen:
            _worktree_proofs.pop(key, None)
    return sorted(worktrees)
