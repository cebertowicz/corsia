/**
 * Keeps `node` scrolled into its nearest scrolling ancestor while `enabled`:
 * once on mount and again whenever the visual viewport or that scroller
 * resizes (an on-screen keyboard, a shortened window, a sheet re-measuring
 * itself), so a field the person is typing into never ends up below the
 * visible edge of a sheet that owns its own scroller.
 */
function scroller(node: HTMLElement): HTMLElement | null {
	for (let parent = node.parentElement; parent; parent = parent.parentElement) {
		if (/auto|scroll/.test(getComputedStyle(parent).overflowY)) return parent;
	}
	return null;
}

export function keepInView(node: HTMLElement, enabled: boolean) {
	let on = enabled;
	let frame = 0;
	const reveal = () => {
		if (on && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' });
	};
	const schedule = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(reveal);
	};
	const viewport = window.visualViewport;
	viewport?.addEventListener('resize', schedule);
	const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
	const owner = scroller(node);
	if (owner) observer?.observe(owner);
	queueMicrotask(reveal);

	return {
		update(next: boolean) {
			on = next;
			reveal();
		},
		destroy() {
			cancelAnimationFrame(frame);
			viewport?.removeEventListener('resize', schedule);
			observer?.disconnect();
		},
	};
}
