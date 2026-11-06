import type { SelectionLabel } from '$lib/stores/commentDraft';

const TOUCH_SLOP = 6;

/** A scroll or horizontal pan is not the tap fallback for a widget comment. */
export const touchMovedBeyond = (start: { x: number; y: number }, current: { x: number; y: number }) => Math.hypot(current.x - start.x, current.y - start.y) > TOUCH_SLOP;

export interface WidgetSelection {
	from: number; to: number; text: string; top: number; left: number;
	anchor: () => DOMRect | null;
	/** Read when a draft is started, from the state the selection was made in: the parse it needs is paid once per comment, not per selection change. */
	label?: SelectionLabel;
	/** Marks a selection made inside a table or diagram widget. */
	inWidget: true;
	/** The Comment button sits on the selection's top edge instead of beside its end, so it never covers text in the selected row. */
	above?: boolean;
}

export function caretAt(x: number, y: number): { node: Node; offset: number } | null {
	const position = document.caretPositionFromPoint?.(x, y);
	if (position) return { node: position.offsetNode, offset: position.offset };
	const range = document.caretRangeFromPoint?.(x, y);
	return range ? { node: range.startContainer, offset: range.startOffset } : null;
}
