/** The gap (px) kept between two stacked margin cards. */
export const GAP = 8;

/** How far (px) the text scrolls up so a card that reaches below the view shows its lower end (its Send button); a card taller than the view lets its top go. */
export function revealShift(card: { bottom: number }, view: { bottom: number }): number {
	const over = card.bottom + GAP - view.bottom;
	return over > 0.5 ? over : 0;
}

/** A margin card: its comment id, the y of its anchor line in document coordinates, and its height. */
export type MarginItem = { id: string; y: number; h: number };

/**
 * Where each margin card sits: beside its text, stacked downward in document order where a cluster would overlap.
 * The selected card keeps its own line and the cards around it step aside, above and below, in the same order.
 */
export function placeCards(items: MarginItem[], selected: string | null): Record<string, number> {
	const out: Record<string, number> = {};
	const at = items.findIndex((item) => item.id === selected);
	let bottom = -Infinity;
	if (at >= 0) {
		out[items[at].id] = items[at].y;
		let limit = items[at].y - GAP;
		for (let i = at - 1; i >= 0; i--) { const top = Math.min(items[i].y, limit - items[i].h); out[items[i].id] = top; limit = top - GAP; }
		bottom = items[at].y + items[at].h + GAP;
	}
	for (let i = at >= 0 ? at + 1 : 0; i < items.length; i++) { const top = Math.max(items[i].y, bottom); out[items[i].id] = top; bottom = top + items[i].h + GAP; }
	return out;
}
