/**
 * Keeps `node` scrolled into its nearest scrolling ancestor while `enabled`:
 * once on mount and again whenever the visual viewport resizes (an on-screen
 * keyboard, a shortened window), so a field the person is typing into never
 * ends up below the visible edge of a sheet that owns its own scroller.
 */
export function keepInView(node: HTMLElement, enabled: boolean) {
	let on = enabled;
	const reveal = () => {
		if (on && typeof node.scrollIntoView === 'function') node.scrollIntoView({ block: 'nearest' });
	};
	const viewport = window.visualViewport;
	viewport?.addEventListener('resize', reveal);
	queueMicrotask(reveal);

	return {
		update(next: boolean) {
			on = next;
			reveal();
		},
		destroy() {
			viewport?.removeEventListener('resize', reveal);
		},
	};
}
