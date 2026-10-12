// Motion for the marks and tags over an image (ImageRegionLayer): Svelte
// transition and animate functions on the motion tokens, run as Web Animations
// so a fade or glide that turns back starts where the last one reached.

function motion() {
	const root = getComputedStyle(document.documentElement);
	return { duration: Number.parseFloat(root.getPropertyValue('--dur-tight')) || 0, easing: root.getPropertyValue('--ease-snap').trim() || 'ease-out' };
}

/**
 * `still()` says when a tag placed somewhere new moves at once rather than
 * gliding: reduced motion, or a zoom or resize that placed every tag again.
 */
export function regionMotion(still: () => boolean) {
	/** Marks and tags fade in where they appear and out where they leave, on the motion tokens, rather than popping. */
	const fades = new WeakMap<Element, Animation>();
	function fade(node: Element, _params: unknown, { direction }: { direction: 'in' | 'out' | 'both' }) {
		const { duration, easing } = motion();
		const opacity = () => { const value = Number.parseFloat(getComputedStyle(node).opacity); return Number.isNaN(value) ? 1 : value; };
		// A fade that turns back starts where the last one reached.
		const previous = fades.get(node);
		const from = previous ? opacity() : null;
		previous?.cancel();
		const rest = opacity();
		const frames = direction === 'out' ? [{ opacity: from ?? rest }, { opacity: 0 }] : [{ opacity: from ?? 0 }, { opacity: rest }];
		fades.set(node, node.animate(frames, { duration, easing, fill: direction === 'out' ? 'forwards' : 'none' }));
		return { duration };
	}
	/** A tag placed somewhere new because a neighbour came, went or moved glides there; with reduced motion it moves at once. */
	const glides = new WeakMap<Element, Animation>();
	function glide(node: Element, { from, to }: { from: DOMRect; to: DOMRect }) {
		glides.get(node)?.cancel();
		if (still()) return { duration: 0 };
		const { duration, easing } = motion();
		glides.set(node, node.animate([{ translate: `${from.left - to.left}px ${from.top - to.top}px` }, { translate: '0px 0px' }], { duration, easing }));
		return { duration };
	}
	return { fade, glide };
}
