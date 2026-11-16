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


def _line_trim(old: str, new: str) -> tuple[int, int, int]:
    """``_trim`` with both cuts moved onto line starts, so a changed block holds whole lines.

    Trimmed to the character, a deleted line whose first words match the next line's keeps those
    letters as the next line's, and a comment on the deleted line lands there.
    """
    prefix, old_end, new_end = _trim(old, new)
    prefix = old.rfind("\n", 0, prefix) + 1
    boundary = old.find("\n", max(old_end - 1, 0))
    boundary = len(old) if boundary == -1 else boundary + 1
    return prefix, boundary, new_end + boundary - old_end


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


LINE_ALIKE = .5


def _alike(left: str, right: str) -> float:
    """How much of two lines is shared; 0 unless more than half is."""
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    if matcher.real_quick_ratio() <= LINE_ALIKE or matcher.quick_ratio() <= LINE_ALIKE:
        return 0
    ratio = matcher.ratio()
    return ratio if ratio > LINE_ALIKE else 0


def _pair_lines(before: list[str], after: list[str]) -> list[tuple[str | None, str | None]]:
    """Pair a changed block's lines in order, most alike first; an unpaired line is a whole delete or insert.

    A character is kept only inside a line paired with its own line. A line that left no alike line
    behind keeps no letter in another, so a comment on it is left with no text.
    """
    if len(before) == len(after) == 1:
        return [(before[0], after[0])]
    best = [[0.0] * (len(after) + 1) for _ in range(len(before) + 1)]
    alike = {}
    for row in reversed(range(len(before))):
        for column in reversed(range(len(after))):
            alike[row, column] = _alike(before[row], after[column])
            paired = best[row + 1][column + 1] + alike[row, column] if alike[row, column] else 0.0
            best[row][column] = max(best[row + 1][column], best[row][column + 1], paired)
    pairs: list[tuple[str | None, str | None]] = []
    row = column = 0
    while row < len(before) and column < len(after):
        if alike[row, column] and best[row][column] == best[row + 1][column + 1] + alike[row, column]:
            pairs.append((before[row], after[column]))
            row += 1
            column += 1
        elif best[row][column] == best[row + 1][column]:
            pairs.append((before[row], None))
            row += 1
        else:
            pairs.append((None, after[column]))
            column += 1
    pairs += [(line, None) for line in before[row:]] + [(None, line) for line in after[column:]]
    return pairs


def _block_changes(before: list[str], after: list[str], start: int):
    """The character changes that turn one changed block of whole lines into another."""
    if max(sum(map(len, before)), sum(map(len, after))) > MAX_CHARACTER_BLOCK and len(before) + len(after) > 2:
        return [(start, start + sum(map(len, before)), "".join(after))]
    changes, position = [], start
    for left, right in _pair_lines(before, after):
        if left is not None and right is not None:
            changes += _character_changes(left, right, position)
        elif left is not None:
            changes.append((position, position + len(left), ""))
        else:
            changes.append((position, position, right))
        position += len(left or "")
    return changes


def _changes(old: str, new: str):
    prefix, old_end, new_end = _line_trim(old, new)
    if prefix == len(old) == len(new):
        return
    before = old[prefix:old_end].splitlines(keepends=True)
    after = new[prefix:new_end].splitlines(keepends=True)
    old_edges, new_edges = _edges(before), _edges(after)
    cursor = byte_cursor = 0
    for tag, first, last, new_first, new_last in _line_opcodes(before, after):
        if tag == "equal":
            continue
        start = prefix + old_edges[first]
        for first, last, inserted in _block_changes(before[first:last], after[new_first:new_last], start):
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
