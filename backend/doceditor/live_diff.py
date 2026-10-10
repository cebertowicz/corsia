"""Pure string diff preparation; no CRDT object crosses the worker boundary."""
import asyncio
from difflib import SequenceMatcher
from time import perf_counter

# Character refinement is bounded independently of document size. A larger
# changed block is replaced as a block, retaining all concurrent CRDT items.
MAX_CHARACTER_BLOCK = 4096
PREPARATION_SLICE_S = .008


def _trim(old: str, new: str) -> tuple[int, int, int]:
    prefix = 0
    common = min(len(old), len(new))
    while prefix < common and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < common - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    return prefix, len(old) - suffix, len(new) - suffix


def _edges(lines: list[str]) -> list[int]:
    result = [0]
    for line in lines:
        result.append(result[-1] + len(line))
    return result


def _line_opcodes(before: list[str], after: list[str]):
    """Align unique, ordered matches once; ambiguous alignments keep the matcher.

    When every common line has one position and those positions increase, all
    equal blocks are forced. SequenceMatcher emits these same blocks but can
    repeatedly scan the remaining document to rediscover each one.
    """
    positions = {}
    for index, line in enumerate(after):
        positions[line] = -1 if line in positions else index
    seen, previous = set(), -1
    for line in before:
        if line not in positions:
            continue
        matched = positions[line]
        if matched < 0 or line in seen or matched <= previous:
            yield from SequenceMatcher(a=before, b=after).get_opcodes()
            return
        seen.add(line)
        previous = matched
    old = new = first = second = length = 0
    # Stream forced blocks instead of allocating one tuple per matching line.
    for index, line in enumerate(before):
        other = positions.get(line)
        if other is None:
            continue
        if length and index == first + length and other == second + length:
            length += 1
            continue
        if length:
            yield "equal", first, first + length, second, second + length
            old, new = first + length, second + length
        if old < index or new < other:
            tag = "replace" if old < index and new < other else "delete" if old < index else "insert"
            yield tag, old, index, new, other
        first, second, length = index, other, 1
    if length:
        yield "equal", first, first + length, second, second + length
        old, new = first + length, second + length
    if old < len(before) or new < len(after):
        tag = "replace" if old < len(before) and new < len(after) else "delete" if old < len(before) else "insert"
        yield tag, old, len(before), new, len(after)


def _character_changes(old: str, new: str, start: int):
    prefix, old_end, new_end = _trim(old, new)
    left, right = old[prefix:old_end], new[prefix:new_end]
    if not left and not right:
        return []
    if max(len(left), len(right)) > MAX_CHARACTER_BLOCK:
        return [(start + prefix, start + old_end, right)]
    return [(start + prefix + first, start + prefix + last, right[new_first:new_last])
            for tag, first, last, new_first, new_last in SequenceMatcher(a=left, b=right).get_opcodes()
            if tag != "equal"]


def _changes(old: str, new: str):
    prefix, old_end, new_end = _trim(old, new)
    if prefix == len(old) == len(new):
        return
    before = old[prefix:old_end].splitlines(keepends=True)
    after = new[prefix:new_end].splitlines(keepends=True)
    old_edges, new_edges = _edges(before), _edges(after)
    cursor = byte_cursor = 0
    for tag, first, last, new_first, new_last in _line_opcodes(before, after):
        if tag == "equal":
            continue
        start, end = prefix + old_edges[first], prefix + old_edges[last]
        replacement = new[prefix + new_edges[new_first]:prefix + new_edges[new_last]]
        for first, last, inserted in _character_changes(old[start:end], replacement, start):
            # Encode each span once, never each changed edge's whole prefix.
            byte_cursor += len(old[cursor:first].encode("utf-8"))
            end_byte = byte_cursor + len(old[first:last].encode("utf-8"))
            yield byte_cursor, end_byte, inserted
            cursor, byte_cursor = last, end_byte


def text_changes(old: str, new: str) -> list[tuple[int, int, str]]:
    """Return UTF-8 opcodes, refining characters only in bounded changed blocks."""
    return list(_changes(old, new))


def _next_batch(changes):
    deadline = perf_counter() + PREPARATION_SLICE_S
    batch = []
    for change in changes:
        batch.append(change)
        if perf_counter() >= deadline:
            return batch, False
    return batch, True


async def text_changes_async(old: str, new: str) -> list[tuple[int, int, str]]:
    """Drain the same pure iterator serially, releasing worker GIL between batches.

    Only strings and opcode preparation state cross the thread boundary. No
    worker survives the awaited batch, and no CRDT object is touched there.
    """
    changes, result = _changes(old, new), []
    while True:
        batch, done = await asyncio.to_thread(_next_batch, changes)
        result.extend(batch)
        if done:
            return result
        await asyncio.sleep(0)
