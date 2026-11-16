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
 * Where each margin card sits: beside its text, stacked downward in document order where a cluster would overlap, and
 * never above the column's top (0), where nothing can scroll to it. What heads the column (its Accept all) is the first item, at 0.
 * The selected card keeps its own line, as in a word processor's margin: the cards above it step up, in the same order,
 * only as far as they must to fit above it; the cards below stack under it. Where the cards above it cannot all fit
 * between the column's top and its line, they stack down from the top and the selected card follows them.
 */
export function placeCards(items: MarginItem[], selected: string | null): Record<string, number> {
	const out: Record<string, number> = {};
	const at = items.findIndex((item) => item.id === selected);
	const above = items.slice(0, Math.max(at, 0)).reduce((sum, item) => sum + item.h + GAP, 0);
	const picked = at >= 0 ? Math.max(items[at].y, above) : 0;
	// The lowest top each card above the selected one can take and still leave the ones after it room above that card.
	const latest: number[] = [];
	for (let i = at - 1, limit = picked; i >= 0; i--) { limit -= items[i].h + GAP; latest[i] = limit; }
	let bottom = 0;
	for (let i = 0; i < items.length; i++) {
		const top = i === at ? picked : Math.min(Math.max(items[i].y, bottom), latest[i] ?? Infinity);
		out[items[i].id] = top;
		bottom = top + items[i].h + GAP;
	}
	return out;
}

/**
 * Focuses a field in a margin card once the card is placed: the one rule for every field the margin column focuses (a new
 * composer, a reply handed off from another form). A card stays hidden until it has a place, which waits for its column to be
 * drawn on the canvas, and a browser drops focus on a hidden field; the stack marks a placed card `placed`. The field takes focus
 * only if the reader has not moved on: focus is still on `from` (where it was when the field opened), on nothing, or on a node that
 * is gone. The wait ends with the placement, or with the stop it returns.
 */
export function focusWhenPlaced(field: HTMLElement, from: Element | null): () => void {
	const card = field.closest<HTMLElement>('[data-margin-card]');
	const free = () => { const now = document.activeElement; return !now || now === document.body || now === from || !now.isConnected; };
	const focus = () => { if (free()) field.focus({ preventScroll: true }); };
	if (!card || card.classList.contains('placed')) { focus(); return () => {}; }
	const placed = new MutationObserver(() => { if (card.classList.contains('placed')) { placed.disconnect(); focus(); } });
	placed.observe(card, { attributes: true, attributeFilter: ['class'] });
	return () => placed.disconnect();
}
