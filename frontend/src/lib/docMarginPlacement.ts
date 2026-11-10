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
 * The selected card keeps its own line and the cards around it step aside, above and below, in the same order. No card
 * goes above `floor` (the column's top, or the foot of what heads it): a card the step-up would lift past the floor
 * stays on it and pushes the cards below it down, and the selected card leaves its line only when they reach it.
 */
export function placeCards(items: MarginItem[], selected: string | null, floor = 0): Record<string, number> {
	const out: Record<string, number> = {};
	const at = items.findIndex((item) => item.id === selected);
	let bottom = floor;
	if (at >= 0) {
		let limit = items[at].y - GAP;
		for (let i = at - 1; i >= 0; i--) { const top = Math.min(items[i].y, limit - items[i].h); out[items[i].id] = top; limit = top - GAP; }
		for (let i = 0; i < at; i++) { const top = Math.max(out[items[i].id], bottom); out[items[i].id] = top; bottom = top + items[i].h + GAP; }
		out[items[at].id] = Math.max(items[at].y, bottom);
		bottom = out[items[at].id] + items[at].h + GAP;
	}
	for (let i = at >= 0 ? at + 1 : 0; i < items.length; i++) { const top = Math.max(items[i].y, bottom); out[items[i].id] = top; bottom = top + items[i].h + GAP; }
	return out;
}
