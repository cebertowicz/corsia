/**
 * Closes a document's Details panel the way a menu closes: a press outside the panel and its button, or Escape from inside
 * either of them. Returns the function that removes both listeners.
 */
export function dismissDetailsOnOutside(panel: () => HTMLElement | null, entry: () => HTMLElement | null, close: () => void): () => void {
	const outside = (event: PointerEvent) => {
		const node = event.target as Node | null;
		if (!node || panel()?.contains(node) || entry()?.contains(node)) return;
		// A picker or review list opened from a row is drawn at the page's top level; pressing in it keeps the panel and that row.
		if ((node instanceof Element ? node : node.parentElement)?.closest('[data-popover], [data-adaptive-drawer]')) return;
		close();
	};
	// Escape closes it only from the panel or its button; anywhere else it belongs to that control (a composer, a sheet).
	const keys = (event: KeyboardEvent) => {
		const node = event.target as Node | null;
		if (event.key !== 'Escape' || !node || !(panel()?.contains(node) || entry()?.contains(node))) return;
		event.preventDefault();
		event.stopPropagation();
		close();
		entry()?.focus();
	};
	document.addEventListener('pointerdown', outside, true);
	document.addEventListener('keydown', keys, true);
	return () => { document.removeEventListener('pointerdown', outside, true); document.removeEventListener('keydown', keys, true); };
}
