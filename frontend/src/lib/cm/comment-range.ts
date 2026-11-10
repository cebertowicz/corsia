import type { ChangeDesc } from '@codemirror/state';

/** One change of an edit, in the text before it: `[from, to)` removed and `inserted` characters put in its place. */
export type TextChange = { from: number; to: number; inserted: number };
/** A comment's text range, and whether its end is open (below). */
export type MappedRange = { from: number; to: number; open: boolean };

/** An edit that only inserts, exactly at `at`. */
export const onlyInsertsAt = (changes: TextChange[], at: number) => changes.every((change) => change.from === at && change.to === at);

/** Whether one change of the edit replaces exactly `[from, to)` with new text: the highlight selected and typed over. */
export const replacesExactly = (changes: TextChange[], from: number, to: number) =>
	from < to && changes.some((change) => change.from === from && change.to === to && change.inserted > 0);

/** The editor's changes, merged where they touch, in the text before them. */
export function editorChanges(changes: ChangeDesc): TextChange[] {
	const out: TextChange[] = [];
	changes.iterChangedRanges((fromA, toA, fromB, toB) => out.push({ from: fromA, to: toA, inserted: toB - fromB }));
	return out;
}

/**
 * Where a comment's text range lands after an edit; the browser's half of the rule the server's live ranges follow
 * (`backend/doceditor/live_text.py` `follow_spans`), and the live placement's (`stores/docCommentPositions.ts`). Its edges
 * are exclusive: text typed exactly at either edge lands outside it. An edit that replaces exactly its text (the highlight
 * selected and typed over) moves the comment onto the new text and opens its end: while the edits after it only insert
 * exactly at that end (the rest of the word being typed), they join it; the first edit anywhere else closes it again.
 * Removing the text with nothing in its place, or replacing more than it, leaves an empty range, the comment whose text is
 * gone. A point (`from === to`) stays a point, before anything typed at it.
 */
export function mapCommentRange(range: { from: number; to: number; open?: boolean }, changes: ChangeDesc): MappedRange {
	const { from, to } = range;
	if (from >= to) { const at = changes.mapPos(from, -1); return { from: at, to: at, open: false }; }
	const edit = editorChanges(changes);
	if (range.open && onlyInsertsAt(edit, to)) return { from, to: changes.mapPos(to, 1), open: true };
	if (replacesExactly(edit, from, to)) return { from: changes.mapPos(from, -1), to: changes.mapPos(to, 1), open: true };
	const kept = { from: changes.mapPos(from, 1), to: changes.mapPos(to, -1) };
	return { from: kept.from, to: Math.max(kept.from, kept.to), open: false };
}
