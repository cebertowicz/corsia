export type ToastFit = 'full' | 'no-time' | 'wrap';

const TOLERANCE = 1;

function overflows(node: HTMLElement): boolean {
	return node.scrollWidth > node.clientWidth + TOLERANCE;
}

/**
 * Settle how a pinned progress line fits its row, in the order the design
 * gives: the whole line on one row, then without the ticking time, then the
 * step text wrapped onto a second line. Never an ellipsis.
 *
 * The result is written to `data-fit`, which the stylesheet reads. Each state
 * is applied and measured before the browser paints, so only the last shows.
 */
export function settleToastFit(node: HTMLElement): ToastFit {
	for (const fit of ['full', 'no-time'] as const) {
		node.dataset.fit = fit;
		if (!overflows(node)) return fit;
	}
	node.dataset.fit = 'wrap';
	return 'wrap';
}

/**
 * Svelte action: keep a pinned toast's `data-fit` current as its content
 * (the parameter changes whenever the message or time does) and its available
 * width change.
 */
export function toastFit(node: HTMLElement, _content?: unknown) {
	const settle = () => {
		settleToastFit(node);
	};
	settle();
	const parent = node.parentElement;
	const observer =
		typeof ResizeObserver === 'undefined' || !parent ? null : new ResizeObserver(settle);
	if (parent) observer?.observe(parent);
	window.addEventListener('resize', settle);
	return {
		update: settle,
		destroy() {
			observer?.disconnect();
			window.removeEventListener('resize', settle);
		},
	};
}
