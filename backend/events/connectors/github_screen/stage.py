"""Stage one outside PR through the script-run GitHub broker."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from floor import diff_paths, edited_item_version, floor_hits, workflow_paths


def github_read(path: str, *, accept: str | None = None) -> str:
    args = ["corsia", "github", "read", path]
    if accept:
        args += ["-H", f"Accept: {accept}"]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        detail = result.stderr.strip()[:1000]
        raise RuntimeError(f"GitHub read of {path} failed: {detail}" if detail else f"GitHub read of {path} failed")
    return result.stdout


def _object_sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", value):
        raise ValueError("GitHub returned an invalid object SHA")
    return value


def _tree(read, prefix: str, sha: str) -> list[dict]:
    data = json.loads(read(f"{prefix}/trees/{sha}"))
    if data.get("truncated"):
        raise ValueError("GitHub returned an incomplete workflow tree")
    return data["tree"]


def workflow_texts(repo: str, revision: str, read) -> list[str]:
    """Read workflow blobs at an exact commit without putting their filenames in API paths."""
    prefix = f"repos/{repo}/git"
    commit = json.loads(read(f"{prefix}/commits/{_object_sha(revision)}"))
    tree = _object_sha(commit["tree"]["sha"])
    for component in (".github", "workflows"):
        entries = _tree(read, prefix, tree)
        entry = next((e for e in entries if e["path"] == component and e["type"] == "tree"), None)
        if entry is None:
            return []
        tree = _object_sha(entry["sha"])
    entries = _tree(read, prefix, tree)
    texts = []
    for entry in entries:
        if entry["type"] == "blob" and entry["path"].endswith((".yml", ".yaml")):
            blob = json.loads(read(f"{prefix}/blobs/{_object_sha(entry['sha'])}"))
            if blob.get("encoding") != "base64":
                raise ValueError("GitHub returned an unsupported workflow encoding")
            texts.append(base64.b64decode(blob["content"]).decode("utf-8"))
    return texts


def stage(event: dict, project: str, root: Path, tracked_repo: str | None, read=github_read) -> dict:
    """Copy the exact PR head to a unique directory and return its event facts."""
    facts = event["facts"]
    repo, number, version = facts["repo"], facts["pr_number"], facts["item_version"]
    if not isinstance(repo, str) or not isinstance(tracked_repo, str) or repo.casefold() != tracked_repo.casefold():
        raise ValueError("event repository does not match the tracked repository")
    if type(number) is not int or number <= 0:
        raise ValueError("PR number must be an integer")
    if not isinstance(version, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", version):
        raise ValueError("head SHA must be 40 hexadecimal characters")
    prefix = f"repos/{repo}/pulls/{number}"
    metadata = json.loads(read(prefix))
    if metadata.get("head", {}).get("sha") != version:
        raise RuntimeError("the PR head moved before staging")
    base_sha = metadata["base"]["sha"]
    if not isinstance(base_sha, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", base_sha):
        raise ValueError("base SHA must be 40 hexadecimal characters")
    diff = read(f"repos/{repo}/compare/{base_sha}...{version}", accept="application/vnd.github.v3.diff")
    ci_paths = workflow_paths(diff, workflow_texts(repo, base_sha, read) + workflow_texts(repo, version, read))
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="screen-", dir=root))
    try:
        (directory / "pull-request.json").write_text(json.dumps(metadata, ensure_ascii=False))
        (directory / "diff.txt").write_text(diff)
        floor = floor_hits(diff, ci_paths=ci_paths)
        (directory / "floor.json").write_text(json.dumps({"floor_hit": sorted(floor),
                                                          "paths": sorted(diff_paths(diff))}))
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return {"repo": tracked_repo, "pr_number": number, "item_version": version,
            "request_identity": hashlib.sha256(
                json.dumps([project, repo.casefold(), number, version.lower()]).encode()).hexdigest(),
            "staging_path": directory.name, "source": "outside"}


def stage_item(event: dict, project: str, root: Path, tracked_repo: str | None, read=github_read) -> dict:
    """Stage the exact issue, comment or review version through the same read broker."""
    facts, kind = event["facts"], event["type"].removeprefix("github.")
    repo, version = facts["repo"], facts["item_version"]
    if not isinstance(repo, str) or not isinstance(tracked_repo, str) or repo.casefold() != tracked_repo.casefold():
        raise ValueError("event repository does not match the tracked repository")
    number = facts.get("pr_number") or facts.get("issue_number")
    if type(number) is not int or number <= 0:
        raise ValueError("item number must be an integer")
    prefix = f"repos/{repo}"
    item_id = str(version).split(":", 1)[0]
    if not item_id.isdecimal():
        raise ValueError("item id must be numeric")
    if kind == "issue.opened":
        endpoint = f"{prefix}/issues/{number}"
    elif kind.startswith("review."):
        endpoint = f"{prefix}/pulls/{number}/reviews/{item_id}"
    elif kind.startswith("issue_comment.") or facts.get("issue_number"):
        endpoint = f"{prefix}/issues/comments/{item_id}"
    else:
        endpoint = f"{prefix}/pulls/comments/{item_id}"
    text = read(endpoint)
    item = json.loads(text)
    if kind.endswith(".edited"):
        current = edited_item_version({**item, "id": item_id}, review=kind.startswith("review."))
        if current != str(version):
            raise RuntimeError("the item changed before staging")
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="triage-", dir=root))
    try:
        (directory / "item.json").write_text(text)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    identity = hashlib.sha256(json.dumps([project, endpoint.casefold(), version]).encode()).hexdigest()
    return {"repo": tracked_repo, "number": number, "item_version": str(version),
            "request_identity": identity, "staging_path": directory.name, "source": "outside"}


def main() -> None:
    event = json.load(sys.stdin)
    context = event["context"]
    handler = stage_item if context["event_type"].endswith(".item_staged") else stage
    facts = handler(event, context["project_id"], Path(context["staging_root"]), context["github_repo"])
    print(json.dumps({"event": {"type": context["event_type"], "facts": facts}}))


if __name__ == "__main__":
    main()
