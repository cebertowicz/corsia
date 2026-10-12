/**
 * Room for the review bar's floating capsule (ReviewBar: a touch screen at a narrow canvas) below a viewer, so the
 * viewer's canvas, and its keyboard focus ring, end above the capsule instead of running under it.
 *
 * The clearance is how far the capsule's top reaches up into `host`, plus the capsule's own side inset as the gap
 * between them. It is zero while the capsule sits in the bar's own row, is tucked away, or is not there.
 */
export function capsuleClearance(host: HTMLElement, viewportHeight = window.innerHeight): number {
	const capsule = host.querySelector<HTMLElement>('.rb-capsule');
	if (!capsule || capsule.classList.contains('suppressed')) return 0;
	const style = getComputedStyle(capsule);
	if (style.position !== 'fixed') return 0;
	// From its layout box, not its rect: the capsule slides in by a transform, which would move the measured top.
	const top = viewportHeight - (Number.parseFloat(style.bottom) || 0) - capsule.offsetHeight;
	const reach = host.getBoundingClientRect().bottom - top;
	return reach > 0 ? Math.round(reach + (Number.parseFloat(style.left) || 0)) : 0;
}

/** Keeps `--capsule-clearance` on `host` current as the capsule appears, grows, tucks away or the window resizes. */
export function trackCapsuleClearance(host: HTMLElement) {
	let capsule: HTMLElement | null = null;
	let frame = 0;
	const measure = () => {
		frame = 0;
		const next = host.querySelector<HTMLElement>('.rb-capsule');
		if (next !== capsule) {
			if (capsule) sizes?.unobserve(capsule);
			if (next) sizes?.observe(next);
			capsule = next;
		}
		host.style.setProperty('--capsule-clearance', `${capsuleClearance(host)}px`);
	};
	// Class changes inside the viewer are frequent (hovered tags): measure at most once a frame.
	const update = () => { if (!frame) frame = requestAnimationFrame(measure); };
	const sizes = typeof ResizeObserver === 'function' ? new ResizeObserver(update) : null;
	const changes = typeof MutationObserver === 'function' ? new MutationObserver(update) : null;
	changes?.observe(host, { childList: true, subtree: true, attributes: true, attributeFilter: ['class'] });
	window.addEventListener('resize', update);
	measure();
	return {
		destroy() {
			if (frame) cancelAnimationFrame(frame);
			changes?.disconnect();
			sizes?.disconnect();
			window.removeEventListener('resize', update);
		},
	};
}
