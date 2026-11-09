/**
 * Draws the document's margin column inside the editor's scrolling content, in the room the editor keeps on the right of
 * its text, so the cards are part of the page: they scroll with their lines, and a wheel or trackpad over them scrolls the
 * document. The element gets `on-canvas` once it is there. The editor is replaced when the document reloads; the column
 * follows it into the new one. Moved out of its parent, the element is no longer removed with an enclosing branch, so it
 * removes itself when its own ends (after any outro).
 */
export function marginOnCanvas(node: HTMLElement, canvas: HTMLElement | null) {
	let observer: MutationObserver | null = null;
	const place = () => {
		const parent = node.parentElement;
		if (parent?.isConnected && parent.classList.contains('cm-scroller')) return;
		const scroller = canvas?.querySelector<HTMLElement>('.cm-scroller');
		if (scroller) scroller.append(node);
		node.classList.toggle('on-canvas', !!scroller);
	};
	const start = (next: HTMLElement | null) => {
		observer?.disconnect();
		canvas = next;
		place();
		if (!canvas) return;
		observer = new MutationObserver(place);
		observer.observe(canvas, { childList: true, subtree: true });
	};
	start(canvas);
	return {
		update: start,
		destroy() { observer?.disconnect(); node.remove(); },
	};
}
