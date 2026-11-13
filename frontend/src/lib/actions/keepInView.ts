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
 * behaviour.
 */
export function keepInView(node: HTMLElement, options: { enabled: boolean; whole?: HTMLElement }) {
	let { enabled, whole } = options;
	let frame = 0;
	const owner = scroller(node);
	const reveal = () => {
		if (!enabled) return;
		const fits = whole !== undefined && owner !== null && whole.getBoundingClientRect().height + scrollMargins(whole) <= owner.clientHeight;
		// A field too tall to bring its whole card along still brings the bordered block it sits in, so its border and Send are not left half under the scroller's edge.
		const block = node.closest<HTMLElement>('[data-autosize-sticky]');
		const blockFits = block !== null && owner !== null && block.getBoundingClientRect().height <= owner.clientHeight;
		const target = fits ? whole! : blockFits ? block! : node;
		// A field that scrolls with its card and has grown taller than the card's scroller cannot be shown whole: its bottom edge (the line being typed, the border and Send) is what stays in view, and the card alone scrolls.
		const tooTall = owner !== null && !fits && !blockFits && block !== null && block.classList.contains('unpinned');
		if (typeof target.scrollIntoView === 'function') (tooTall ? block! : target).scrollIntoView({ block: tooTall ? 'end' : 'nearest' });
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
	// Content above the node that changes height moves it without resizing it: the scroller's own children are watched too, and again whenever the content under the scroller is replaced or restyled (a region above bounded or released), so a region above growing brings the node back into view.
	const watchChildren = () => { if (owner) for (const child of owner.children) observer?.observe(child); };
	watchChildren();
	const mutations = owner && typeof MutationObserver === 'function' ? new MutationObserver(() => { watchChildren(); schedule(); }) : null;
	mutations?.observe(owner!, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] });
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
			mutations?.disconnect();
		},
	};
}
