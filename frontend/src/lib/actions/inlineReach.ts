/**
 * Where the measurement comes from: the bar the controls sit in, and who hears how far in they reach.
 * `controlsWidth` is the caller's own reading of the controls' width: passing it means a render — and so a
 * fresh measurement — happens when they change size, wherever the caller's size binding comes from.
 */
export type InlineReachParams = { shell?: HTMLElement | null; controlsWidth?: number; report?: (width: number) => void };

/**
 * Reports how far `controls` reach in from `shell`'s inline end (the header's right controls, so the canvas tabs can stop
 * before them), now and whenever either box changes size. The controls are watched by their border box: a padding change
 * (the overlaid drawer's) moves their reach without changing their content.
 *
 * The reading is the canvas's only clearance from the header's controls, and an early one says "no clearance" — the first
 * render can measure a header whose controls have not arrived with the scope yet. So every later render reads again: a
 * control appearing under the same shell changes no box this observer watches, and a stale reading would leave the
 * canvas's buttons sitting under the gear.
 */
export function inlineReach(controls: HTMLElement, params: InlineReachParams) {
	let current = params;
	let observer: ResizeObserver | null = null;
	let measure: (() => void) | null = null;
	function start() {
		observer?.disconnect();
		observer = null;
		measure = null;
		const shell = current.shell;
		// The reading stands on its own; the observer only keeps it current between renders.
		measure = shell ? () => current.report?.(Math.ceil(shell.getBoundingClientRect().right - controls.getBoundingClientRect().left)) : null;
		if (!measure) return;
		measure();
		if (typeof ResizeObserver === 'undefined') return;
		observer = new ResizeObserver(measure);
		observer.observe(shell);
		observer.observe(controls, { box: 'border-box' });
	}
	start();
	return {
		update(next: InlineReachParams) {
			const moved = next.shell !== current.shell;
			current = next;
			if (moved) start();
			else measure?.();
		},
		destroy() {
			observer?.disconnect();
			measure = null;
		},
	};
}
