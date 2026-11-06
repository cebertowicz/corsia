import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';

/**
 * An image's drawer slides with the grid track its viewer animates, so it stays mounted while the track closes; with
 * reduced motion it crossfades instead. A document's side drawer (a canvas wide enough for a column) appears and leaves
 * at once; its sheet form uses AdaptiveDrawer's open/onClosed lifecycle.
 */
export function imageDrawerMotion(isImage: boolean, leaving: boolean) {
	if (!isImage) return { duration: 0 };
	const token = (name: string) => Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
	const pace = leaving ? 0.7 : 1;
	if (get(prefersReducedMotion)) return { duration: token('--dur-tight') * pace, css: (t: number) => `opacity: ${t}` };
	return { duration: leaving ? token('--dur-drawer') * pace : 0 };
}
