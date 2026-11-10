import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';

/**
 * The margin column leaves fading: the section stays mounted for the spring duration, and its `leaving` class makes the
 * editor scroller's right padding close over the same time (DocViewer.css). Reduced motion has no animation.
 */
export function marginOut(node: Element) {
	if (get(prefersReducedMotion)) return { duration: 0 };
	node.classList.add('leaving');
	return { duration: Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur-spring')) || 380, css: (t: number) => `opacity: ${t}` };
}
