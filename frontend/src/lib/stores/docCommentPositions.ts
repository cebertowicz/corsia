import * as Y from 'yjs';
import { onlyInsertsAt, type TextChange } from '$lib/cm/comment-range';

/**
 * A live range's exclusive edges, and its inclusive ones (`around`) for the edit that types over all of its text. `open`: its
 * text was just typed over, so the rest of the word typed exactly at its end joins it (`cm/comment-range.ts`).
 */
export type Positions = { from: Y.RelativePosition; to: Y.RelativePosition; around: { from: Y.RelativePosition; to: Y.RelativePosition }; open: boolean };

/** Exclusive boundaries: text typed exactly at either edge lands outside the range; the inclusive pair takes it in. */
export function edges(text: Y.Text, from: number, to: number, open = false): Positions {
	const at = (index: number, assoc: number) => Y.createRelativePositionFromTypeIndex(text, index, assoc);
	return { from: at(from, 0), to: at(to, -1), around: { from: at(from, -1), to: at(to, 0) }, open };
}

/** A Yjs edit as changes in the text before it (UTF-16 indices, like the rows'), a delete and the insert beside it merged. */
export function deltaChanges(delta: Y.YTextEvent['delta']): TextChange[] {
	const out: TextChange[] = [];
	let at = 0, last: TextChange | undefined;
	for (const op of delta) {
		if (op.retain) { at += op.retain; last = undefined; continue; }
		if (!last) out.push(last = { from: at, to: at, inserted: 0 });
		if (op.delete) { last.to += op.delete; at += op.delete; }
		if (op.insert !== undefined) last.inserted += typeof op.insert === 'string' ? op.insert.length : 1;
	}
	return out;
}

/**
 * Where a range held at `at` is now, by the same rule as the server's ranges (`backend/doceditor/live_text.py` `map_spans`)
 * and the editor's (`cm/comment-range.ts`): all of its text typed over moves it onto what was typed and opens its end, which
 * takes in edits that only insert exactly there; any other edit closes it. Deleted with nothing in its place, it is empty.
 * `was` is the range before the edit, `delta` the edit; `moved` holds the positions to keep when they change.
 */
export function placeNow(at: Positions, text: Y.Text, was: { from?: number; to?: number }, delta: Y.YTextEvent['delta']): { from?: number; to?: number; moved?: Positions } {
	const index = (position: Y.RelativePosition) => Y.createAbsolutePositionFromRelativePosition(position, text.doc!)?.index;
	if (at.open && was.to !== undefined && onlyInsertsAt(deltaChanges(delta), was.to)) {
		const from = index(at.from), to = index(at.around.to);
		return from === undefined || to === undefined ? { from, to } : { from, to, moved: edges(text, from, to, true) };
	}
	const from = index(at.from), to = index(at.to);
	const closed = at.open ? { ...at, open: false } : undefined;
	if (from === undefined || to === undefined || from < to || was.from === undefined || was.to === undefined || was.from >= was.to) return { from, to, moved: closed };
	const wider = index(at.around.from), widerTo = index(at.around.to);
	return wider !== undefined && widerTo !== undefined && wider < widerTo ? { from: wider, to: widerTo, moved: edges(text, wider, widerTo, true) } : { from, to, moved: closed };
}
