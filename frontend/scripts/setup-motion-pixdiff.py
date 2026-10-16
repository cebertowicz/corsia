#!/usr/bin/env python3
"""Fail when designated setup-motion PNG frames differ by less than 1%.

Reports each pair's exact differing-pixel count and percentage. Uses only the
standard library so the app-path Playwright evidence can run from the worktree's
normal test environment without another image dependency.
"""

from __future__ import annotations

import argparse
import itertools
import struct
import sys
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def pixels(path: Path) -> tuple[int, int, bytes]:
    raw = path.read_bytes()
    if not raw.startswith(PNG_SIGNATURE):
        raise ValueError(f"{path} is not a PNG")
    width = height = bit_depth = color_type = None
    chunks: list[bytes] = []
    offset = len(PNG_SIGNATURE)
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        kind = raw[offset + 4 : offset + 8]
        data = raw[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, bit_depth, color_type, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
            if compression or filtering or interlace or bit_depth != 8 or color_type not in (2, 6):
                raise ValueError(f"{path} has unsupported PNG encoding")
        elif kind == b"IDAT":
            chunks.append(data)
        elif kind == b"IEND":
            break
    if width is None or height is None or bit_depth is None or color_type is None:
        raise ValueError(f"{path} has no IHDR")
    channels = 4 if color_type == 6 else 3
    stride = width * channels
    decoded = zlib.decompress(b"".join(chunks))
    rows: list[bytes] = []
    previous = bytearray(stride)
    cursor = 0
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        row = bytearray(decoded[cursor : cursor + stride])
        cursor += stride
        for index, value in enumerate(row):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                row[index] = (value + left) & 255
            elif filter_type == 2:
                row[index] = (value + above) & 255
            elif filter_type == 3:
                row[index] = (value + ((left + above) // 2)) & 255
            elif filter_type == 4:
                row[index] = (value + paeth(left, above, upper_left)) & 255
            elif filter_type != 0:
                raise ValueError(f"{path} has unsupported PNG filter {filter_type}")
        rows.append(bytes(row))
        previous = row
    return width, height, b"".join(rows)


def differing_pixels(a: bytes, b: bytes, channels: int) -> int:
    return sum(a[index : index + channels] != b[index : index + channels] for index in range(0, len(a), channels))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("frames", nargs="+", type=Path)
    args = parser.parse_args()
    loaded = {frame: pixels(frame) for frame in args.frames}
    failures: list[str] = []
    for left, right in itertools.combinations(args.frames, 2):
        left_width, left_height, left_pixels = loaded[left]
        right_width, right_height, right_pixels = loaded[right]
        if (left_width, left_height) != (right_width, right_height):
            print(f"distinct by size: {left.name} / {right.name}")
            continue
        channels = 4 if len(left_pixels) == left_width * left_height * 4 else 3
        count = differing_pixels(left_pixels, right_pixels, channels)
        total = left_width * left_height
        ratio = count / total
        print(f"frame difference: {left.name} / {right.name}: {count}/{total} ({ratio:.2%})")
        if ratio < 0.01:
            failures.append(f"{left.name} and {right.name} differ by only {ratio:.2%} (minimum 1.00%)")
    if failures:
        print("FAIL: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("PASS: every designated frame pair differs by at least 1.00%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
