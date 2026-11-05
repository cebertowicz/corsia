import { slide } from 'svelte/transition';
import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';
import { carryPanelHeight } from './settleHeight';

/**
 * Enters or leaves something inside a card the way inserted rows do: its size eases open or shut (`axis` y for a line,
 * x for an icon in a row) while it fades with it, so a card whose state changes under the person never pops. With reduced
 * motion nothing slides: it only fades, over `--dur-tight`. A sheet around it is told the size is already easing
 * (`carryPanelHeight`), so it never replays the change itself.
 */
export function clipFade(node: HTMLElement, { axis = 'y' }: { axis?: 'x' | 'y' } = {}) {
	if (get(prefersReducedMotion)) {
		const tight = Number.parseFloat(getComputedStyle(node).getPropertyValue('--dur-tight')) || 120;
		return { duration: tight, css: (t: number) => `opacity: ${t}` };
	}
	const base = slide(node, { axis, duration: 180 });
	// Svelte starts only a placeholder animation now, and the real one from the placeholder's finish handler, which can run
	// a frame after the placeholder's `finished` promise settles. So the sheet is told at once, and the hold waits for the
	// placeholder's own finish (or cancel) event, which comes after that handler, then for everything animating on the node.
	if (axis === 'y') queueMicrotask(() => {
		if (typeof node.getAnimations !== 'function') return;
		const running = () => node.getAnimations().filter((item) => Number.isFinite(item.effect?.getComputedTiming().endTime));
		const placeholders = running().map((item) => new Promise((done) => { item.addEventListener('finish', done, { once: true }); item.addEventListener('cancel', done, { once: true }); }));
		carryPanelHeight(node, Promise.all(placeholders).then(() => Promise.all(running().map((item) => item.finished.catch(() => {})))));
	});
	const css = base.css!;
	return { ...base, css: (t: number, u: number) => `${css(t, u)};opacity: ${t}` };
}
