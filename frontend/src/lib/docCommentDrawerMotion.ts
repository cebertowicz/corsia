import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';

/**
 * The margin column leaves fading: the section stays mounted for the spring duration while the editor scroller's right
 * padding closes over the same time (DocViewer.css, from the surface's `marginShown`). Reduced motion has no animation.
 */
export function marginOut(_node: Element) {
	if (get(prefersReducedMotion)) return { duration: 0 };
	return { duration: Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur-spring')) || 380, css: (t: number) => `opacity: ${t}` };
}
