/**
 * The longest start of `head` that, with a "…" after it, is no wider than `available` (`widthOf` measures a string).
 * A start that already fits comes back as it is, without an ellipsis.
 */
export function fitPrefix(head: string, widthOf: (text: string) => number, available: number): string {
	if (widthOf(head) <= available) return head;
	const chars = Array.from(head);
	let fits = 0;
	let over = chars.length;
	while (fits < over) {
		const mid = Math.ceil((fits + over) / 2);
		if (widthOf(`${chars.slice(0, mid).join('')}…`) <= available) fits = mid;
		else over = mid - 1;
	}
	return `${chars.slice(0, fits).join('')}…`;
}

/**
 * Sets the start of a file name that is cut in the middle so the "…" sits against the kept end with no gap. The browser's own
 * `text-overflow` draws the "…" after the last letter that fits and leaves up to a letter of empty room after it, so this
 * lays the chip out with the whole start (the room it leaves is then known), then writes the longest start that fits with a
 * real "…" and no spare room. It runs again when the list the chip sits in changes width. Call it on the span holding the start.
 */
export function fitMiddleEllipsis(node: HTMLElement, head: string) {
	const text = node.firstChild;
	if (!(text instanceof Text) || typeof ResizeObserver === 'undefined' || typeof document.createRange().getBoundingClientRect !== 'function') return {};
	const range = document.createRange();
	range.selectNodeContents(node);
	let start = head;
	const widthOf = (value: string) => { text.data = value; return range.getBoundingClientRect().width; };
	const fit = () => {
		text.data = start;
		const available = node.getBoundingClientRect().width;
		if (available <= 0) return;
		text.data = fitPrefix(start, widthOf, available);
	};
	// The list's width is what limits a chip; the chip's own width follows its text and must not be watched.
	const observer = new ResizeObserver(fit);
	observer.observe(node.closest('ul') ?? document.documentElement);
	fit();
	void document.fonts?.ready.then(fit);
	return {
		update(next: string) { start = next; fit(); },
		destroy() { observer.disconnect(); },
	};
}
