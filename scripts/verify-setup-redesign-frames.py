#!/usr/bin/env python3
"""Validate the setup-redesign evidence pack after a source-rendered capture."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image

FRAME_NAME = re.compile(r"^(?P<state>.+)-(?P<theme>light|dark)-(?P<size>\d+x\d+)\.png$")
EMBED = re.compile(r"!\[[^\]]*\]\(([^)]+\.png)\)")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_aliases(path: Path) -> dict[frozenset[str], str]:
    raw = json.loads(path.read_text())
    aliases: dict[frozenset[str], str] = {}
    for canonical, value in raw.items():
        if not isinstance(value, dict) or not isinstance(value.get("aliases"), list) or not value.get("reason"):
            raise ValueError(f"{canonical}: aliases must name aliases and a reason")
        for alias in value["aliases"]:
            aliases[frozenset((canonical, alias))] = value["reason"]
    return aliases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--without-aliases", action="store_true")
    args = parser.parse_args()

    doc = args.doc.read_text()
    assets = args.assets
    manifest = json.loads((assets / "frames.json").read_text())
    frames = sorted(assets.glob("*.png"))
    errors: list[str] = []

    if any("![" in line and "|" in line for line in doc.splitlines()):
        errors.append("a Markdown image is inside a table row")

    references = [Path(value).name for value in EMBED.findall(doc)]
    files = {path.name for path in frames}
    for filename in sorted(files):
        count = references.count(filename)
        if count != 1:
            errors.append(f"{filename}: embedded {count} times")
    for filename in sorted(set(references) - files):
        errors.append(f"{filename}: embedded but missing from assets")

    records = {record.get("file"): record for record in manifest}
    if set(records) != files:
        errors.append("frames.json and PNG inventory differ")
    for filename in sorted(files):
        record = records.get(filename, {})
        if not record.get("assertion"):
            errors.append(f"{filename}: no recorded state-specific assertion")
        if record.get("sha256") != sha256(assets / filename):
            errors.append(f"{filename}: bytes do not match frames.json SHA-256")

    source = args.spec.read_text()
    if "await assertState(page, fixture);" not in source:
        errors.append("fixture captures do not call state-specific Playwright assertions")
    if "Scripted first-run opening" not in source or "getByText(openingLead" not in source:
        errors.append("chat capture lacks its state-specific Playwright assertion")

    aliases = {} if args.without_aliases else load_aliases(assets / "aliases.json")
    groups: dict[tuple[str, str], list[tuple[str, Path]]] = defaultdict(list)
    for path in frames:
        match = FRAME_NAME.match(path.name)
        if not match:
            errors.append(f"{path.name}: unsupported frame filename")
            continue
        groups[(match.group("theme"), match.group("size"))].append((match.group("state"), path))

    pair_count = 0
    distinct_passes = 0
    declared_aliases: list[tuple[str, str, str]] = []
    for group in groups.values():
        decoded = [(state, Image.open(path).convert("RGBA").tobytes()) for state, path in group]
        for (left_state, left), (right_state, right) in itertools.combinations(decoded, 2):
            pair_count += 1
            if left != right:
                distinct_passes += 1
                continue
            reason = aliases.get(frozenset((left_state, right_state)))
            if reason:
                declared_aliases.append((left_state, right_state, reason))
            else:
                errors.append(f"pixel-identical frames: {left_state} and {right_state}")

    print(f"PIXEL CHECK: {pair_count} comparable pairs; {distinct_passes} distinct pairs; {len(declared_aliases)} declared aliases.")
    for left, right, reason in declared_aliases:
        print(f"ALIAS: {left} = {right} — {reason}")
    if errors:
        print("FAILED:", *errors, sep="\n- ", file=sys.stderr)
        return 1
    print(f"PASS: {len(files)} images are embedded once, asserted, and byte-matched to frames.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
