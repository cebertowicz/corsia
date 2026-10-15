// Motion for a menu that opens from its anchor and for a list that swaps with its peer: motion.md's "Popover, menu or
// tooltip" and "Switch between peers" rows. Svelte transitions run as Web Animations on the motion tokens, an exit at 70%
// of its entrance. With reduced motion a menu only fades. Use them as `in:menuPop out:menuPop` and `in:peerFade out:peerFade`:
// `transition:` tells them 'both' either way, so they could not know which way to run.
import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';

export const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
/** A duration token in milliseconds (`--dur-standard`), read from the document so the tokens stay the one source. */
export const ms = (name: string) => Number.parseFloat(cssVar(name)) || 0;

type Direction = { direction: 'in' | 'out' | 'both' };

function run(node: HTMLElement, from: Keyframe, to: Keyframe, { direction }: Direction) {
	if (direction === 'both') throw new Error('menuPop and peerFade run one way: use in: and out:, not transition:');
	const entering = direction === 'in';
	const duration = ms('--dur-tight') * (entering ? 1 : 0.7);
	node.animate(entering ? [from, to] : [to, from], { duration, easing: cssVar('--ease-snap') || 'ease-out', fill: entering ? 'none' : 'forwards' });
	return { duration };
}

/** A menu fades in from its anchor (4px, 0.98 to 1); `origin` is the corner of the menu the anchor touches. */
export function menuPop(node: HTMLElement, params: { origin?: string } | undefined, options: Direction) {
	if (params?.origin) node.style.transformOrigin = params.origin;
	const closed = get(prefersReducedMotion) ? { opacity: 0 } : { opacity: 0, transform: 'translateY(-4px) scale(0.98)' };
	return run(node, closed, { opacity: 1, transform: 'none' }, options);
}

/** One of two peers (a filtered list, a tab's body) fades in as the other fades out, in the same place. */
export function peerFade(node: HTMLElement, _params: undefined, options: Direction) {
	return run(node, { opacity: 0 }, { opacity: 1 }, options);
}
