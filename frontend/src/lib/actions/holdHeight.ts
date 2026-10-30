/**
 * Keeps `node` from getting shorter while `key` stays the same, so a region that swaps between states of different
 * heights (reasoning text, a quiet-gap indicator, a tool list) at the end of a scroller never pulls the end of that
 * scroller up. A reader pinned to the end would otherwise be moved by every shortening, then moved back when the
 * region grows again.
 *
 * The floor is set after the layout that made the node its tallest, so it is already in place when the content
 * shrinks. A new `key` releases it and starts again from the node's height after the change; the caller puts into
 * the key every change that is meant to shorten the node (content leaving for the transcript, a person's toggle).
 */
export function holdHeight(node: HTMLElement, key: string) {
	let current = key;
	let peak = 0;
	let frame = 0;
	const hold = () => {
		const height = node.getBoundingClientRect().height;
		if (height <= peak) return;
		peak = height;
		node.style.minHeight = `${height}px`;
	};
	const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(hold);
	observer?.observe(node);
	return {
		update(next: string) {
			if (next === current) return;
			current = next;
			peak = 0;
			node.style.minHeight = '';
			cancelAnimationFrame(frame);
			frame = requestAnimationFrame(hold);
		},
		destroy() {
			cancelAnimationFrame(frame);
			observer?.disconnect();
		},
	};
}
