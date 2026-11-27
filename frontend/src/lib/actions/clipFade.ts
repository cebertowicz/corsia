import { slide } from 'svelte/transition';
import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';

export function clipFadeDuration(node?: Element): number {
	if (!get(prefersReducedMotion)) return 180;
	return (node && Number.parseFloat(getComputedStyle(node).getPropertyValue('--dur-tight'))) || 120;
}

/**
 * Enters or leaves something inside a card the way inserted rows do: its size eases open or shut (`axis` y for a line,
 * x for an icon in a row) while it fades with it, so a card whose state changes under the person never pops. With reduced
 * motion nothing slides: it only fades, over `--dur-tight`.
 */
export function clipFade(node: HTMLElement, { axis = 'y' }: { axis?: 'x' | 'y' } = {}) {
	if (get(prefersReducedMotion)) {
		return { duration: clipFadeDuration(node), css: (t: number) => `opacity: ${t}` };
	}
	const base = slide(node, { axis, duration: clipFadeDuration(node) });
	const css = base.css!;
	return { ...base, css: (t: number, u: number) => `${css(t, u)};opacity: ${t}` };
}
