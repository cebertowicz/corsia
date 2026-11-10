/** Where a margin column is drawn: the surface it belongs to, and the selector of the scroller inside it that holds the content. */
export type MarginCanvas = { root: HTMLElement | null; scroller: string };

/**
 * Draws a margin column inside its surface's scrolling content (a document's editor, a chat's transcript), in the room
 * beside the content, so the cards are part of the page: they scroll with their lines, and a wheel or trackpad over them
 * scrolls the content. The element gets `on-canvas` once it is there. A document's editor is replaced when the document
 * reloads; the column follows it into the new one. Moved out of its parent, the element is no longer removed with an
 * enclosing branch, so it removes itself when its own ends (after any outro).
 */
export function marginOnCanvas(node: HTMLElement, canvas: MarginCanvas) {
	let observer: MutationObserver | null = null;
	const place = () => {
		const parent = node.parentElement;
		if (parent?.isConnected && parent.matches(canvas.scroller)) return;
		const scroller = canvas.root?.querySelector<HTMLElement>(canvas.scroller);
		if (scroller) scroller.append(node);
		node.classList.toggle('on-canvas', !!scroller);
	};
	const start = (next: MarginCanvas) => {
		observer?.disconnect();
		canvas = next;
		place();
		if (!canvas.root) return;
		observer = new MutationObserver(place);
		// A document's editor sits a level or two down, so the whole subtree is watched for its replacement.
		observer.observe(canvas.root, { childList: true, subtree: true });
	};
	start(canvas);
	return {
		update: start,
		destroy() { observer?.disconnect(); node.remove(); },
	};
}
