/** Where the measurement comes from: the bar the controls sit in, and who hears how far in they reach. */
export type InlineReachParams = { shell?: HTMLElement | null; report?: (width: number) => void };

/**
 * Reports how far `controls` reach in from `shell`'s inline end (the header's right controls, so the canvas tabs can stop
 * before them), now and whenever either box changes size. The controls are watched by their border box: a padding change
 * (the overlaid drawer's) moves their reach without changing their content.
 */
export function inlineReach(controls: HTMLElement, params: InlineReachParams) {
	let current = params;
	let observer: ResizeObserver | null = null;
	function start() {
		observer?.disconnect();
		observer = null;
		const shell = current.shell;
		if (!shell || typeof ResizeObserver === 'undefined') return;
		const update = () => current.report?.(Math.ceil(shell.getBoundingClientRect().right - controls.getBoundingClientRect().left));
		update();
		observer = new ResizeObserver(update);
		observer.observe(shell);
		observer.observe(controls, { box: 'border-box' });
	}
	start();
	return {
		update(next: InlineReachParams) {
			const moved = next.shell !== current.shell;
			current = next;
			if (moved) start();
		},
		destroy() { observer?.disconnect(); },
	};
}
