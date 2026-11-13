/** The nearest ancestor that scrolls on its own, or null when the page itself is the scroller. */
export function scroller(node: HTMLElement): HTMLElement | null {
	for (let parent = node.parentElement; parent; parent = parent.parentElement) {
		if (/auto|scroll/.test(getComputedStyle(parent).overflowY)) return parent;
	}
	return null;
}

/** The block's own scroll margins: `scrollIntoView` leaves them clear, so they spend from the scroller's room. */
function scrollMargins(element: HTMLElement): number {
	const style = getComputedStyle(element);
	return (parseFloat(style.scrollMarginTop) || 0) + (parseFloat(style.scrollMarginBottom) || 0);
}

/**
 * Keeps `node` scrolled into its nearest scrolling ancestor while `enabled`:
 * once on mount and again whenever the visual viewport or that scroller
 * resizes (an on-screen keyboard, a shortened window, a sheet re-measuring
 * itself), so a field the person is typing into never ends up below the
 * visible edge of a sheet that owns its own scroller.
 *
 * `whole` is the block `node` belongs to. While that block, including the scroll
 * margins it asks a scroll to leave clear, fits the scroller it is revealed
 * instead of the node, so bringing a field into view never pushes the top of the
 * block it sits at the end of out of sight above and never clips the ring drawn
 * around that block; a block too tall for the scroller keeps the node-into-view
 * behaviour, and a node itself taller than the scroller shows its end.
 */
export function keepInView(node: HTMLElement, options: { enabled: boolean; whole?: HTMLElement }) {
	let { enabled, whole } = options;
	let frame = 0;
	const owner = scroller(node);
	const reveal = () => {
		if (!enabled) return;
		const room = owner?.clientHeight ?? window.visualViewport?.height ?? window.innerHeight;
		const fits = whole !== undefined && owner !== null && whole.getBoundingClientRect().height + scrollMargins(whole) <= owner.clientHeight;
		const target = fits ? whole! : node;
		// A node taller than the room shows its end, where the caret of a growing field and its Send are.
		const block = !fits && node.getBoundingClientRect().height + scrollMargins(node) > room ? 'end' : 'nearest';
		if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block });
	};
	const schedule = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(reveal);
	};
	const viewport = window.visualViewport;
	viewport?.addEventListener('resize', schedule);
	const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
	if (owner) observer?.observe(owner);
	observer?.observe(node);
	queueMicrotask(reveal);

	return {
		update(next: { enabled: boolean; whole?: HTMLElement }) {
			({ enabled, whole } = next);
			reveal();
		},
		destroy() {
			cancelAnimationFrame(frame);
			viewport?.removeEventListener('resize', schedule);
			observer?.disconnect();
		},
	};
}
