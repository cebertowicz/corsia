"""Pixel-difference check for the cloud-first setup frames, measured on the final PNG files.

Counted frames are every PNG in <assetsDir> except the --skip names. The check fails (exit 1) when
  - two counted frames of the same size have a mean absolute difference (over all pixels and channels, 0-255 scale)
    below MIN_MEAN, or differ in fewer than MIN_PIXELS pixels (a duplicate or near-duplicate),
  - a counted frame has no index line in the doc (- `name.png`: its state-specific element) or an empty one, or
  - the doc names a frame file that is not in the folder.
Frames of different sizes are distinct by construction and are listed, not compared.

Usage: cloud-first-pixdiff.py <assetsDir> <doc.md> [--skip a.png,b.png]   run the check, print the table
       cloud-first-pixdiff.py <assetsDir> <doc.md> --selftest             prove it fails on an exact copy, a near-copy and a frame
                                                                           with no index line
       cloud-first-pixdiff.py <assetsDir> <doc.md> --corrupt              run on a copy of the set with one frame replaced by a
                                                                           near-copy of another (expect exit 1)
Run it with `python -I` (needs Pillow and numpy)."""
import itertools
import os
import re
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image

MIN_MEAN = 0.02   # mean absolute difference, 0-255: two renders of one frame differ by 0; one changed control is well above
MIN_PIXELS = 200  # differing pixels: an unchanged frame with one moved word still passes; a copy does not
# Frames that show one state reached by two routes: they must be pixel-identical (0 differing pixels), so they are
# exempt from the distinctness rule and fail instead if a single pixel differs.
IDENTICAL_PAIRS = [('p-own-omlx-note.png', 'p-starter-note.png'), ('p-own-omlx-note-dark.png', 'p-starter-note-dark.png')]


def load(path):
    return np.asarray(Image.open(path).convert('RGB'), dtype=np.int16)


FRAME_LINE = re.compile(r'^\s*[-*]\s+`([\w.\-]+\.png)`\s*:\s*(.*)$')


def doc_rows(doc_text):
    """file name -> the state-specific element text from the doc's index line for it: - `name.png`: element."""
    rows = {}
    for line in doc_text.splitlines():
        match = FRAME_LINE.match(line)
        if not match:
            continue
        name, element = match.group(1), match.group(2).strip()
        if not re.search(r'[A-Za-z]{4}', element):
            element = ''
        rows.setdefault(name, element)
    return rows


def check(folder, files, doc_text):
    failures, rows, identical = [], [], []
    refs = doc_rows(doc_text)
    for f in files:
        if f not in refs:
            failures.append(f'{f} has no index line in the doc')
        elif not refs[f]:
            failures.append(f'{f} has an index line but no state-specific element')
    for name in refs:
        if not os.path.exists(os.path.join(folder, name)):
            failures.append(f'the doc names {name}, which is not in the folder')
    imgs = {f: load(os.path.join(folder, f)) for f in files}
    for a, b in IDENTICAL_PAIRS:
        if a not in imgs or b not in imgs:
            continue
        if imgs[a].shape != imgs[b].shape:
            failures.append(f'{a} and {b} must be identical but differ in size')
            continue
        changed = np.any(imgs[a] != imgs[b], axis=2)
        if changed.any():
            ys, xs = np.nonzero(changed)
            failures.append(f'{a} and {b} must be identical: {int(changed.sum())} differing pixels in bbox ({xs.min()},{ys.min()})-({xs.max()},{ys.max()})')
        else:
            identical.append((a, b))
    for a, b in itertools.combinations(files, 2):
        if imgs[a].shape != imgs[b].shape or (a, b) in IDENTICAL_PAIRS or (b, a) in IDENTICAL_PAIRS:
            continue
        d = np.abs(imgs[a] - imgs[b])
        mean = float(d.mean())
        pixels = int(np.count_nonzero(np.any(d != 0, axis=2)))
        rows.append((mean, pixels, a, b))
        if mean < MIN_MEAN or pixels < MIN_PIXELS:
            failures.append(f'{a} and {b}: mean difference {mean:.4f}, {pixels} differing pixels (need at least {MIN_MEAN} and {MIN_PIXELS})')
    return failures, sorted(rows), identical


def counted(folder, skip):
    return sorted(f for f in os.listdir(folder) if f.endswith('.png') and f not in skip)


def selftest(folder, files, doc_text):
    """Prove the check fails on each defect it exists to catch."""
    with tempfile.TemporaryDirectory() as tmp:
        sub = [f for f in files if '1280x900' in f][:6]
        for f in sub:
            shutil.copy(os.path.join(folder, f), tmp)
        doc_sub = '\n'.join(line for line in doc_text.splitlines() if any(f'`{f}`' in line for f in sub))
        twin = 'twin-' + sub[0]
        shutil.copy(os.path.join(tmp, sub[0]), os.path.join(tmp, twin))
        fails, _, _ = check(tmp, sub + [twin], doc_sub + f'\n- `{twin}`: Twin element')
        assert any(twin in x and 'mean difference' in x for x in fails), 'the check did not flag an exact duplicate'
        near = 'near-' + sub[0]
        arr = load(os.path.join(tmp, sub[0])).astype(np.uint8)
        arr[0:3, 0:3] = 255 - arr[0:3, 0:3]
        Image.fromarray(arr).save(os.path.join(tmp, near))
        fails, _, _ = check(tmp, sub + [near], doc_sub + f'\n- `{near}`: Near element')
        assert any(near in x and 'mean difference' in x for x in fails), 'the check did not flag a near-duplicate'
        nodoc = 'nodoc-' + sub[1]
        arr = np.roll(load(os.path.join(tmp, sub[1])).astype(np.uint8), 40, axis=0)
        Image.fromarray(arr).save(os.path.join(tmp, nodoc))
        fails, _, _ = check(tmp, sub + [nodoc], doc_sub)
        assert any(nodoc in x and 'no index line' in x for x in fails), 'the check did not flag a frame with no index line'
        noel = 'noel-' + sub[2]
        arr = np.roll(load(os.path.join(tmp, sub[2])).astype(np.uint8), 60, axis=0)
        Image.fromarray(arr).save(os.path.join(tmp, noel))
        fails, _, _ = check(tmp, sub + [noel], doc_sub + f'\n- `{noel}`: ')
        assert any(noel in x and 'no state-specific element' in x for x in fails), 'the check did not flag a row without its element'
    with tempfile.TemporaryDirectory() as tmp:
        a, b = IDENTICAL_PAIRS[0]
        pair = [a, b]
        for f in pair:
            shutil.copy(os.path.join(folder, f), tmp)
        doc_pair = '\n'.join(line for line in doc_text.splitlines() if any(f'`{f}`' in line for f in pair))
        fails, _, same = check(tmp, pair, doc_pair)
        assert not fails and same == [(a, b)], f'an identical pair was not accepted: {fails}'
        arr = load(os.path.join(tmp, a)).astype(np.uint8)
        arr[10, 10] = 255 - arr[10, 10]
        Image.fromarray(arr).save(os.path.join(tmp, a))
        fails, _, _ = check(tmp, pair, doc_pair)
        assert any('must be identical: 1 differing pixels' in x for x in fails), 'the check did not flag a one-pixel difference'
    print('selftest ok: an exact copy, a 9-pixel change, a frame with no index line, an index line with no element and a one-pixel difference between the frames that must match are each flagged')


def main():
    folder, doc = sys.argv[1], sys.argv[2]
    skip = set(sys.argv[sys.argv.index('--skip') + 1].split(',')) if '--skip' in sys.argv else set()
    doc_text = open(doc, encoding='utf-8').read()
    files = counted(folder, skip)
    if '--selftest' in sys.argv:
        selftest(folder, files, doc_text)
        return
    if '--corrupt' in sys.argv:
        with tempfile.TemporaryDirectory() as tmp:
            for f in files:
                shutil.copy(os.path.join(folder, f), tmp)
            sizes = {}
            for f in files:
                sizes.setdefault(load(os.path.join(tmp, f)).shape, []).append(f)
            group = next(g for g in sizes.values() if len(g) > 1)
            a, b = group[0], group[1]
            arr = load(os.path.join(tmp, a)).astype(np.uint8)
            arr[0:2, 0:2] = 255 - arr[0:2, 0:2]
            Image.fromarray(arr).save(os.path.join(tmp, b))
            failures, _, _ = check(tmp, files, doc_text)
        print(f'corrupted set: {b} replaced by a near-copy of {a}')
        for line in failures:
            print(' -', line)
        sys.exit(1 if failures else 0)
    failures, rows, identical = check(folder, files, doc_text)
    sizes = {}
    for f in files:
        sizes.setdefault(load(os.path.join(folder, f)).shape, []).append(f)
    print(f'counted frames: {len(files)}; same-size pairs compared: {len(rows)}')
    for shape, group in sizes.items():
        print(f'  {shape[1]}x{shape[0]}: {len(group)} frames')
    print('closest 8 same-size pairs (mean absolute difference, differing pixels):')
    for mean, pixels, a, b in rows[:8]:
        print(f'  {mean:8.4f}  {pixels:8d}  {a}  vs  {b}')
    if rows:
        print(f'closest pair: mean {rows[0][0]:.4f}, {rows[0][1]} pixels (thresholds {MIN_MEAN} and {MIN_PIXELS})')
    for a, b in identical:
        print(f'identical by design: {a} vs {b}: 0 differing pixels')
    if failures:
        print('FAIL')
        for line in failures:
            print(' -', line)
        sys.exit(1)
    print('PASS: every counted frame differs from every other counted frame of its size and has a doc row with its element')


main()
