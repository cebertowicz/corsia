/**
 * Keeps what a scroller shows from getting shorter while an agent works, and after it stops, until the person leaves
 * the thread. A next send keeps the blank space it finds and gives it back as the new turn's content grows into it, so
 * the send never shortens the scroller under a reader. `node`, the scroller's last child, takes up whatever the rest of the content loses
 * (the live row swapping to a shorter state, a step's tools folding into a row, the waiting dots leaving at the end of
 * the turn) as blank space, and gives it back as that content grows. A reader at the end of the scroller is then never
 * moved by a shortening, which a browser answers by pulling the end up, and moved back when the content grows again.
 *
 * The blank space is set in the same task as the change that made the content shorter (a mutation callback runs before
 * the frame is painted), so no painted frame shows the shorter content. A person's own toggle needs no release: what
 * it shortens is held too, which keeps their place.
 */
export function holdTail(node: HTMLElement, params: { key: string; live: boolean }) {
	const scroller = node.parentElement;
	let current = params.key;
	let live = false;
	let holding = false;
	let high = 0;
	let width = 0;
	let mutations: MutationObserver | null = null;
	let resizes: ResizeObserver | null = null;

	/** The scroller's content height without the blank space this node adds. */
	const content = () => (scroller ? scroller.scrollHeight - node.offsetHeight : 0);
	const settle = () => {
		if (!holding || !scroller) return;
		const now = content();
		if (now > high) high = now;
		const gap = high - now;
		node.style.height = gap >= 1 ? `${gap}px` : '';
	};
	const stop = () => {
		holding = false;
		high = 0;
		mutations?.disconnect();
		resizes?.disconnect();
		mutations = null;
		resizes = null;
		node.style.height = '';
	};
	/** `carry` keeps the blank space already there, so the scroller is no shorter than it was a moment ago. */
	const start = (carry = false) => {
		if (!scroller) return;
		const kept = carry && holding ? node.offsetHeight : 0;
		stop();
		holding = true;
		high = content() + kept;
		if (kept > 0) node.style.height = `${kept}px`;
		width = scroller.clientWidth;
		mutations = new MutationObserver(settle);
		mutations.observe(scroller, { childList: true, subtree: true, characterData: true });
		// A different width reflows the text, so the old height means nothing: start again from the new one.
		resizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
			if (scroller.clientWidth === width) return;
			width = scroller.clientWidth;
			high = content();
			settle();
		});
		resizes?.observe(scroller);
	};

	if (params.live) start();
	live = params.live;
	return {
		update(next: { key: string; live: boolean }) {
			if (next.key !== current) {
				current = next.key;
				live = next.live;
				if (next.live) start(); else stop();
				return;
			}
			if (next.live && !live) start(true);
			live = next.live;
		},
		destroy: stop,
	};
}
