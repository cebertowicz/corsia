import type { EditorView } from '@codemirror/view';

/** Where the text column ends: the rightmost text drawn on a line the canvas shows. The editor itself runs wider, so its own edge would leave no margin for a card. */
function textRight(v: EditorView): number {
	const area = v.scrollDOM.getBoundingClientRect();
	const span = document.createRange();
	let right = 0;
	for (const line of v.contentDOM.querySelectorAll('.cm-line')) {
		span.selectNodeContents(line);
		const drawn = span.getBoundingClientRect();
		if (drawn.width > 0 && drawn.bottom > area.top && drawn.top < area.bottom) right = Math.max(right, drawn.right);
	}
	return right;
}

/** The rows a range is drawn on, out to the text column's edge, so a card can sit clear of all of it. Null while they are not drawn. */
export function rowsOf(v: EditorView, from: number, to: number, suggestionId: string | null = null): DOMRect | null {
	if (from < v.viewport.from || to > v.viewport.to) return null;
	const start = v.coordsAtPos(from, 1), end = v.coordsAtPos(to, 1);
	if (!start || !end) return null;
	// A suggestion's drawn insert follows its struck text and may wrap onto the next row.
	const insert = suggestionId ? v.contentDOM.querySelector(`.cm-suggestion-insert[data-suggestion-id="${suggestionId}"]`)?.getBoundingClientRect() : null;
	const top = Math.min(start.top, end.top, insert?.top ?? Infinity);
	return acrossColumn(v, top, Math.max(start.bottom, end.bottom, insert?.bottom ?? -Infinity), Math.max(start.right, end.right, insert?.right ?? 0));
}

/** Rows from `top` to `bottom`, from the text column's left out to its edge or to `right` (drawn content that reaches further), never past the editor. */
export function acrossColumn(v: EditorView, top: number, bottom: number, right: number): DOMRect {
	const content = v.contentDOM.getBoundingClientRect();
	const edge = Math.min(content.right, Math.max(textRight(v), right));
	return new DOMRect(content.left, top, Math.max(0, edge - content.left), bottom - top);
}
