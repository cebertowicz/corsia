// Motion for a control that arrives in or leaves a row of controls: motion.md's "a row arrives or leaves", turned on
// its side. It grows in where it lands as it fades in; leaving, it fades first, then its space and the row's gap close,
// so the controls beside it slide rather than jump. A Svelte transition run as Web Animations on the motion tokens;
// with reduced motion it only fades. Use it as `in:inlineSlot out:inlineSlot`: `transition:` tells it 'both' either way,
// so it could not know which way to run. `out:inlineSlot={{ instant: true }}` leaves at once, for a control another takes
// the place of, so the two never stand in the row together.
import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';

function token(name: string): string {
	return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const ms = (name: string) => Number.parseFloat(token(name)) || 0;

/** The gap, in pixels, of the row the control sits in, past any wrapper that takes no box of its own. */
function rowGap(node: Element): number {
	let parent = node.parentElement;
	while (parent && getComputedStyle(parent).display === 'contents') parent = parent.parentElement;
	return (parent && Number.parseFloat(getComputedStyle(parent).columnGap)) || 0;
}

export function inlineSlot(node: HTMLElement, params: { instant?: boolean } | undefined, { direction }: { direction: 'in' | 'out' | 'both' }) {
	if (direction === 'both') throw new Error('inlineSlot runs one way: use in:inlineSlot out:inlineSlot, not transition:');
	const entering = direction === 'in';
	if (!entering && params?.instant) return { duration: 0 };
	const fade = ms('--dur-tight');
	const snap = token('--ease-snap') || 'ease-out';
	if (get(prefersReducedMotion)) {
		node.animate([{ opacity: entering ? 0 : 1 }, { opacity: entering ? 1 : 0 }], { duration: fade, easing: snap, fill: entering ? 'none' : 'forwards' });
		return { duration: fade };
	}
	// max-width rather than width, so a control that fills the rest of its row (flex: 1) grows too.
	const open = { maxWidth: `${node.getBoundingClientRect().width}px`, minWidth: '0px', marginInlineStart: '0px', opacity: 1 };
	const closed = { maxWidth: '0px', minWidth: '0px', marginInlineStart: `${-rowGap(node)}px`, opacity: 0 };
	const grow = ms('--dur-standard');
	const entrance = token('--ease-entrance') || 'ease-out';
	node.style.overflow = 'hidden';
	if (entering) {
		const animation = node.animate([closed, open], { duration: grow, easing: entrance });
		void animation.finished.then(() => node.style.removeProperty('overflow'), () => {});
		return { duration: grow };
	}
	const total = fade + grow;
	node.animate(
		[{ ...open, easing: snap }, { ...open, opacity: 0, offset: total ? fade / total : 0, easing: entrance }, closed],
		{ duration: total, fill: 'forwards' },
	);
	return { duration: total };
}
