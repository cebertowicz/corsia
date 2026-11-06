import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';
import { menuPop } from '$lib/popoverMotion';

/**
 * The margin column leaves fading: the section stays mounted for the spring duration, and its `leaving` class makes the
 * document's grid track close over the same time (DocViewer.css). Reduced motion has no animation.
 */
export function marginOut(node: Element) {
	if (get(prefersReducedMotion)) return { duration: 0 };
	node.classList.add('leaving');
	return { duration: Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--dur-spring')) || 380, css: (t: number) => `opacity: ${t}` };
}

/** The list box opens and closes as the filter popup beside it does (`menuPop`, hanging from its entry); with reduced motion it is instant. */
export function asidePop(node: HTMLElement, params: { origin?: string } | undefined, options: { direction: 'in' | 'out' | 'both' }) {
	return get(prefersReducedMotion) ? { duration: 0 } : menuPop(node, params, options);
}
