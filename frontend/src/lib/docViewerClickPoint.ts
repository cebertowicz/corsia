/** The line box under the click: the text rect at the click's height, widened by the line's half-leading. A margin marker's own box stands in for its line. */
function clickedLine(target: Node, y: number): { top: number; bottom: number } | null {
	const el = target instanceof Element ? target : target.parentElement;
	const marker = el?.closest('.cm-comment-marker');
	if (marker) { const box = marker.getBoundingClientRect(); return { top: box.top, bottom: box.bottom }; }
	if (!el) return null;
	const range = document.createRange();
	range.selectNodeContents(el);
	const text = [...range.getClientRects()].filter((rect) => rect.height > 0 && rect.top <= y + 1 && rect.bottom >= y - 1).sort((a, b) => a.height - b.height)[0];
	if (!text) return null;
	const leading = Math.max(0, (parseFloat(getComputedStyle(el).lineHeight) - text.height) / 2) || 0;
	return { top: text.top - leading, bottom: text.bottom + leading };
}

/**
 * The box a card opened by `click` sits against: the clicked line at the pointer's x. Null when no click opened it, for a
 * keyboard press on a button (a click with no pointer, `detail` 0), so the card opens beside the comment's text as before.
 * The box keeps its offset from `live` (the comment's own box), so the card follows its text as it scrolls or moves and
 * hides when `live` does.
 */
export function clickAnchor(click: MouseEvent | undefined, live: () => DOMRect | null): (() => DOMRect | null) | null {
	if (!click || click.detail === 0 || !(click.target instanceof Node)) return null;
	const line = clickedLine(click.target, click.clientY);
	const rect = new DOMRect(click.clientX, line?.top ?? click.clientY, 0, line ? line.bottom - line.top : 0);
	const from = live();
	if (!from) return () => rect;
	return () => {
		const now = live();
		return now && new DOMRect(rect.x + now.left - from.left, rect.y + now.top - from.top, 0, rect.height);
	};
}
