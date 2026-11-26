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
export function fitMiddleEllipsis(node: HTMLElement, name: { head: string; tail: string }) {
	const text = node.firstChild;
	if (!(text instanceof Text) || typeof ResizeObserver === 'undefined' || typeof document.createRange().getBoundingClientRect !== 'function') return {};
	const range = document.createRange();
	range.selectNodeContents(node);
	let { head, tail } = name;
	const widthOf = (value: string) => { text.data = value; return range.getBoundingClientRect().width; };
	const fit = () => {
		const end = node.nextElementSibling?.firstChild;
		const endText = end instanceof Text ? end : null;
		const extensionAt = tail.lastIndexOf('.');
		const removable = Array.from(extensionAt < 0 ? tail : tail.slice(0, extensionAt));
		const extension = extensionAt < 0 ? '' : tail.slice(extensionAt);
		if (endText) endText.data = tail;
		text.data = head;
		if (node.getBoundingClientRect().width <= 0) return;
		for (let removed = 0; ; removed++) {
			text.data = head;
			const available = node.getBoundingClientRect().width;
			text.data = fitPrefix(head, widthOf, available);
			if (node.scrollWidth <= node.clientWidth || !endText || removed >= removable.length) break;
			endText.data = removable.slice(removed + 1).join('') + extension;
		}
	};
	// The list's width is what limits a chip; the chip's own width follows its text and must not be watched.
	const observer = new ResizeObserver(fit);
	observer.observe(node.closest('ul') ?? document.documentElement);
	fit();
	void document.fonts?.ready.then(fit);
	return {
		update(next: { head: string; tail: string }) { ({ head, tail } = next); fit(); },
		destroy() { observer.disconnect(); },
	};
}
