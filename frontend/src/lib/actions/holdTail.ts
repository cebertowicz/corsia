/**
 * Keeps what a scroller shows from getting shorter while an agent works, and after it stops, until the person leaves
 * the thread. The floor only rises while a turn is live: when it ends the floor stays where it reached, so what the
 * person does afterwards (expanding then collapsing a step) behaves as it always did. A next send keeps the blank space
 * it finds and gives it back as the new turn's content grows into it, so
 * the send never shortens the scroller under a reader. `node`, an absolutely placed 1px marker in the scroller, sets a
 * floor under the scroller's height: its bottom edge sits at the tallest the content has been, plus the scroller's
 * bottom padding as it is now (the composer's band), so a band that shrinks shortens the scroller as it always did. Whatever the rest of the
 * content loses (the live row swapping to a shorter state, a step's tools folding into a row, the waiting dots leaving
 * at the end of the turn) is then blank space at the end, and the content grows back into it. The floor is part of
 * layout, so no layout ever shows the scroller shorter and a browser never pulls a reader at the end up; it only moves
 * up when the content grows past it.
 *
 * A person's own toggle during a turn needs no release: what it shortens is held too, which keeps their place.
 */
/**
 * Where the scroller's content ends, as a scrollTop-style height: `scrollHeight` less the blank floor the marker holds.
 * The one place "the latest" is measured, so the jump control, following the live edge and a restored position agree.
 */
export function contentEnd(scroller: HTMLElement): number {
	const marker = scroller.querySelector<HTMLElement>(':scope > .tail-hold');
	const end = scroller.querySelector<HTMLElement>(':scope > .tail-end');
	if (!marker || !end || marker.offsetHeight === 0) return scroller.scrollHeight;
	return Math.min(scroller.scrollHeight, end.offsetTop + (parseFloat(getComputedStyle(scroller).paddingBottom) || 0));
}

export function holdTail(node: HTMLElement, params: { key: string; live: boolean }) {
	const scroller = node.parentElement;
	let current = params.key;
	let live = false;
	let holding = false;
	let high = 0;
	let width = 0;
	let mutations: MutationObserver | null = null;
	let resizes: ResizeObserver | null = null;

	/** Where the content ends, above the scroller's bottom padding; `.tail-end` is the zero-height block that marks it. */
	const endTop = () => scroller?.querySelector<HTMLElement>(':scope > .tail-end')?.offsetTop ?? scroller?.scrollHeight ?? 0;
	/** The marker's CSS puts its bottom edge at `--tail-high` plus the scroller's padding. */
	const place = (height: number) => {
		if (height > 0) node.style.setProperty('--tail-high', `${height}px`);
		else node.style.removeProperty('--tail-high');
		node.style.height = height > 0 ? '1px' : '';
	};
	/** The floor never lets the content end come back higher than `high`; only growth past it moves it. */
	const settle = () => {
		if (!holding || !scroller) return;
		const now = endTop();
		if (now > high) {
			high = now;
			place(high);
		}
	};
	const watch = () => {
		if (!scroller || mutations) return;
		mutations = new MutationObserver(settle);
		mutations.observe(scroller, { childList: true, subtree: true, characterData: true });
	};
	const unwatch = () => {
		mutations?.disconnect();
		mutations = null;
	};
	const stop = () => {
		holding = false;
		high = 0;
		unwatch();
		resizes?.disconnect();
		resizes = null;
		place(0);
	};
	/** `carry` keeps the floor already there, so the scroller is no shorter than it was a moment ago. */
	const start = (carry = false) => {
		if (!scroller) return;
		if (!(carry && holding)) {
			stop();
			holding = true;
			width = scroller.clientWidth;
			// A different width reflows the text, so the old height means nothing: start again from the new one.
			resizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
				if (scroller.clientWidth === width) return;
				width = scroller.clientWidth;
				place(0);
				high = endTop();
				place(high);
			});
			resizes?.observe(scroller);
		}
		watch();
		high = Math.max(carry ? high : 0, endTop());
		place(high);
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
			// The turn ended: take its last growth, then stop raising the floor.
			if (!next.live && live) { settle(); unwatch(); }
			live = next.live;
		},
		destroy: stop,
	};
}
