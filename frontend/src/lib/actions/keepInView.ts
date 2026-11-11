/**
 * Keeps `node` scrolled into its nearest scrolling ancestor while `enabled`:
 * once on mount and again whenever the visual viewport or that scroller
 * resizes (an on-screen keyboard, a shortened window, a sheet re-measuring
 * itself), so a field the person is typing into never ends up below the
 * visible edge of a sheet that owns its own scroller.
 *
 * `whole` is the block `node` belongs to. While that block fits the scroller it
 * is revealed instead of the node, so bringing a field into view never pushes
 * the top of the block it sits at the end of out of sight above; a block too
 * tall for the scroller keeps the node-into-view behaviour.
 */
export function scroller(node: HTMLElement): HTMLElement | null {
	for (let parent = node.parentElement; parent; parent = parent.parentElement) {
		if (/auto|scroll/.test(getComputedStyle(parent).overflowY)) return parent;
	}
	return null;
}

export function keepInView(node: HTMLElement, options: { enabled: boolean; whole?: HTMLElement }) {
	let { enabled, whole } = options;
	let frame = 0;
	const owner = scroller(node);
	const reveal = () => {
		if (!enabled) return;
		const fits = whole !== undefined && owner !== null && whole.getBoundingClientRect().height <= owner.clientHeight;
		const target = fits ? whole! : node;
		if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'nearest' });
	};
	const schedule = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(reveal);
	};
	const viewport = window.visualViewport;
	viewport?.addEventListener('resize', schedule);
	const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
	if (owner) observer?.observe(owner);
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
