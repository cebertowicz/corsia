#!/usr/bin/env python3
"""Detect duplicate static setup frames and write the evidence-pack pixel report.

Frames are captured with reduced motion and CSS pixels. A pair of differently
named states are rejected only when their rendered pixels are exactly equal.
Explicit aliases are permitted only by the supplied mapping.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops

DUPLICATE_RATIO = 0.0


def state_and_viewport(filename: str) -> tuple[str, str, str]:
    state, theme, viewport = Path(filename).stem.rsplit("-", 2)
    return state, theme, viewport


def image(path: Path) -> Image.Image:
    with Image.open(path) as opened:
        return opened.convert("RGB")


def changed_pixels(left: Image.Image, right: Image.Image) -> int:
    if left.size != right.size:
        raise ValueError(f"same labeled viewport has mismatched dimensions: {left.size} / {right.size}")
    difference = ImageChops.difference(left, right).convert("L")
    changed = difference.point(lambda value: 255 if value else 0)
    return changed.histogram()[255]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frames", required=True, type=Path)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--aliases", type=Path)
    args = parser.parse_args()
    records = json.loads(args.frames.read_text())
    aliases = json.loads(args.aliases.read_text()) if args.aliases else {}
    images = {record["file"]: image(args.assets / record["file"]) for record in records}
    pairs: list[dict[str, object]] = []
    failures: list[str] = []
    for left, right in itertools.combinations(records, 2):
        left_state, left_theme, left_viewport = state_and_viewport(left["file"])
        right_state, right_theme, right_viewport = state_and_viewport(right["file"])
        if left_theme != right_theme or left_viewport != right_viewport:
            continue
        count = changed_pixels(images[left["file"]], images[right["file"]])
        total = images[left["file"]].width * images[left["file"]].height
        ratio = count / total
        alias = right_state in aliases.get(left_state, []) or left_state in aliases.get(right_state, [])
        pairs.append({"left": left["file"], "right": right["file"], "different_pixels": count, "total_pixels": total, "difference_ratio": ratio, "explicit_alias": alias})
        if ratio <= DUPLICATE_RATIO and not alias:
            failures.append(f"{left['file']} / {right['file']} are pixel-identical without an explicit alias")
    report = {"static_reduced_motion": True, "duplicate_threshold_ratio": DUPLICATE_RATIO,
        "duplicate_policy": "exact pixel equality fails unless the pair is explicitly aliased",  "frames": len(records), "aliases": aliases, "pairs": pairs, "failures": failures}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if failures:
        print("FAIL: " + "; ".join(failures), file=sys.stderr)
        return 1
    print(f"PASS: {len(records)} frames; {len(pairs)} same-theme/same-size pairs are non-identical or explicit aliases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
