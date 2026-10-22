"""Structural guards for the bundled ``corsia-default`` coordination model.

The package at ``docs/coordination-models/corsia-default`` ships charter and
safety templates, responsibility materials, a routing policy and declared KPIs. Four
invariants keep it installable by a conductor:

1. every fill placeholder in the templates is covered by a manifest parameter
   (its ``parameters[].id`` or one of its ``parameters[].fills`` entries);
2. the pack envelope declares its schema and a semver version, while legacy
   installed copies carrying bare integer ``0`` remain readable;
3. every ``files[]`` pin still matches the file's byte length and sha256;
4. the manifest's ``notes`` list holds strings only.

Each break case runs on a ``tmp_path`` copy of the package with one deliberate
defect, so the guards are shown to fire without touching the repository.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_ROOT / "docs" / "coordination-models" / "corsia-default"

# The two templates whose placeholders the manifest must cover.
_TEMPLATES = ("context/PROJECT.md.tmpl", "context/SAFETY-INVARIANTS.md.tmpl")

_PLACEHOLDER_RE = re.compile(r"<([^<>\n]+)>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SEMVER_RE = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
# Words that name the template-filling convention itself, not project content.
_META_WORDS = frozenset({"parameter", "placeholder", "source"})


def _load_manifest(package_dir: Path) -> dict[str, Any]:
    """Parse the package manifest with ``yaml.safe_load``."""
    with (package_dir / "manifest.yaml").open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    return loaded if isinstance(loaded, dict) else {}


def _version_failures(manifest: dict[str, Any]) -> list[str]:
    """Reject invalid pack versions while accepting installed legacy version 0."""
    version = manifest.get("version")
    if type(version) is int and version == 0:
        return []
    if isinstance(version, str) and _SEMVER_RE.fullmatch(version):
        return []
    return [f"manifest version {version!r} is neither semver nor legacy bare 0"]


def _is_hint(token: str, in_comment: bool) -> bool:
    """True for a hint token rather than a fill placeholder.

    Hints carry prose (a space or a colon), and the filling convention's own
    meta words appear inside HTML comments. Neither is a fill the conductor
    answers, so neither needs a manifest parameter.
    """
    if " " in token or ":" in token:
        return True
    return in_comment and token.strip() in _META_WORDS


def _placeholders(text: str) -> set[str]:
    """Fill placeholders in ``text``, excluding hint and meta tokens."""
    comment_spans = [(m.start(), m.end()) for m in _COMMENT_RE.finditer(text)]
    found: set[str] = set()
    for match in _PLACEHOLDER_RE.finditer(text):
        token = match.group(1)
        in_comment = any(start <= match.start() < end for start, end in comment_spans)
        if not _is_hint(token, in_comment):
            found.add(token.strip())
    return found


def _covered_tokens(manifest: dict[str, Any]) -> set[str]:
    """Tokens a manifest parameter covers, from its id and its fills list."""
    covered: set[str] = set()
    parameters = manifest.get("parameters")
    if not isinstance(parameters, list):
        return covered
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        identifier = parameter.get("id")
        if isinstance(identifier, str):
            covered.add(identifier)
        fills = parameter.get("fills")
        if isinstance(fills, list):
            covered.update(item for item in fills if isinstance(item, str))
    return covered


def _coverage_failures(package_dir: Path) -> list[str]:
    """Fill placeholders no manifest parameter covers, in stable order."""
    covered = _covered_tokens(_load_manifest(package_dir))
    uncovered: set[str] = set()
    for relative in _TEMPLATES:
        text = (package_dir / relative).read_text(encoding="utf-8")
        uncovered.update(_placeholders(text) - covered)
    return [f"<{token}> has no manifest parameter or fills entry" for token in sorted(uncovered)]


def _pin_failures(package_dir: Path) -> list[str]:
    """``files[]`` pins whose byte length or sha256 no longer matches."""
    failures: list[str] = []
    files = _load_manifest(package_dir).get("files")
    if not isinstance(files, list):
        return ["manifest files is not a list"]
    for entry in files:
        if not isinstance(entry, dict):
            failures.append(f"files entry {entry!r} is not a mapping")
            continue
        relative = str(entry.get("path"))
        target = package_dir / relative
        if not target.is_file():
            failures.append(f"{relative}: pinned file missing at {target}")
            continue
        data = target.read_bytes()
        if entry.get("bytes") != len(data):
            failures.append(f"{relative}: pinned bytes {entry.get('bytes')} but file is {len(data)}")
        digest = hashlib.sha256(data).hexdigest()
        if entry.get("sha256") != digest:
            failures.append(f"{relative}: pinned sha256 {entry.get('sha256')} but file is {digest}")
    return failures


def _note_failures(package_dir: Path) -> list[str]:
    """Manifest ``notes`` items that are not strings."""
    notes = _load_manifest(package_dir).get("notes")
    if not isinstance(notes, list):
        return [f"manifest notes is {type(notes).__name__}, not a list"]
    return [
        f"notes[{index}] is {type(item).__name__}, not str: {item!r}"
        for index, item in enumerate(notes)
        if not isinstance(item, str)
    ]


def _copy_package(tmp_path: Path) -> Path:
    """A disposable copy of the package, so a break never touches the repo."""
    package = tmp_path / "corsia-default"
    shutil.copytree(PACKAGE_DIR, package)
    return package


def test_placeholders_are_covered() -> None:
    """The shipped package covers every fill placeholder."""
    assert _coverage_failures(PACKAGE_DIR) == []


def test_manifest_has_a_valid_pack_envelope() -> None:
    """The shipped package declares the standard schema and a semver version."""
    manifest = _load_manifest(PACKAGE_DIR)
    assert manifest.get("schema") == "corsia.pack/1"
    assert isinstance(manifest.get("version"), str)
    assert _version_failures(manifest) == []


def test_legacy_bare_zero_version_remains_readable(tmp_path: Path) -> None:
    """Installed copies from before the envelope use the accepted bare-zero marker."""
    package = _copy_package(tmp_path)
    manifest = package / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('version: "0.1.0"', "version: 0"),
        encoding="utf-8",
    )
    assert _version_failures(_load_manifest(package)) == []


def test_invalid_version_is_rejected(tmp_path: Path) -> None:
    """A version that is neither semver nor the legacy marker is rejected."""
    package = _copy_package(tmp_path)
    manifest = package / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('version: "0.1.0"', 'version: "0.1"'),
        encoding="utf-8",
    )
    assert _version_failures(_load_manifest(package)) == [
        "manifest version '0.1' is neither semver nor legacy bare 0"
    ]


def test_pins_match_their_files() -> None:
    """Every shipped pin matches its file's bytes and sha256."""
    assert _pin_failures(PACKAGE_DIR) == []


def test_notes_are_all_strings() -> None:
    """The shipped manifest's notes list holds strings only."""
    assert _note_failures(PACKAGE_DIR) == []


def test_uncovered_placeholder_fails(tmp_path: Path) -> None:
    """An uncovered ``<snake_case>`` placeholder fires the coverage guard."""
    package = _copy_package(tmp_path)
    template = package / "context" / "PROJECT.md.tmpl"
    template.write_text(
        template.read_text(encoding="utf-8") + "\n<uncovered_placeholder>\n",
        encoding="utf-8",
    )
    failures = _coverage_failures(package)
    assert failures, "coverage guard did not fire on an uncovered placeholder"
    assert failures == ["<uncovered_placeholder> has no manifest parameter or fills entry"]


def test_stale_pin_fails(tmp_path: Path) -> None:
    """Editing a pinned file fires the pin guard for that file alone."""
    package = _copy_package(tmp_path)
    pinned = package / "context" / "COORDINATION-CORE.md"
    pinned.write_text(pinned.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    failures = _pin_failures(package)
    assert failures, "pin guard did not fire on a stale pin"
    # Only the edited entry is stale.
    assert {failure.split(":", 1)[0] for failure in failures} == {
        "context/COORDINATION-CORE.md"
    }


def test_non_string_note_fails(tmp_path: Path) -> None:
    """A non-string item in ``notes`` fires the manifest-shape guard."""
    package = _copy_package(tmp_path)
    manifest = package / "manifest.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "  - 42\n", encoding="utf-8"
    )
    failures = _note_failures(package)
    assert failures, "notes guard did not fire on a non-string note"
    assert failures == ["notes[5] is int, not str: 42"]