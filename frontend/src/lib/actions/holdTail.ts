/**
 * The one owner of the floor under the transcript: the held blank space that keeps a reader at the end from being
 * moved when the content gets shorter (the live row swapping to a shorter state, a step's tools folding into a row,
 * the waiting dots leaving at the end of the turn). A browser clamps `scrollTop` to `scrollHeight`, and only content
 * inside the scroller counts toward `scrollHeight`, so while a floor holds the reader's place `scrollHeight` is the
 * floor and not the content. That is why nothing else reads a transcript's `scrollHeight`: it asks this module, which
 * answers with `contentEnd` (where the content ends) and `anchorPrepend` (older history added above it).
 *
 * `node`, an absolutely placed 1px marker in the scroller, sets the floor: its bottom edge sits at the tallest the
 * content has been, plus the scroller's bottom padding as it is now (the composer's band), so a band that shrinks
 * shortens the scroller as it always did. The floor only rises while a turn is live: when the turn ends it stays where
 * it reached, so what the person does afterwards (expanding then collapsing a step) behaves as it always did. A next
 * send keeps the blank space it finds and gives it back as the new turn's content grows into it. The floor is part of
 * layout, so no layout ever shows the scroller shorter; it only moves up when the content grows past it. A person's own
 * toggle during a turn needs no release: what it shortens is held too, which keeps their place. When the band under the
 * content shrinks while the view reaches into the blank below the content, the view comes down with it, as the browser's
 * own clamp would have if the floor were not there.
 */
export interface Scroller {
	scrollTop: number;
	readonly scrollHeight: number;
	readonly clientHeight: number;
}

interface Floor {
	/** Where the content ends, as a scrollTop-style height (`scrollHeight` less the held blank space). */
	end(): number;
	/** Hold `blank` px under the content as it is now. */
	hold(blank: number): void;
}

const floors = new WeakMap<object, Floor>();

/** Where the scroller's content ends, as a scrollTop-style height: the one place "the latest" is measured. */
export function contentEnd(scroller: Scroller): number {
	return floors.get(scroller)?.end() ?? scroller.scrollHeight;
}

/**
 * Where a view that sits `offset` px above the latest content ends up (0 follows it): measured from the content's end,
 * or from the plain bottom where nothing is held. A view at or past the bottom of unheld content is at its plain bottom.
 */
export function followTop(scroller: Scroller, offset = 0): number {
	const end = contentEnd(scroller);
	if (end < scroller.scrollHeight) return Math.max(0, end - scroller.clientHeight - offset);
	return offset <= 0 ? scroller.scrollHeight : Math.max(0, scroller.scrollHeight - scroller.clientHeight - offset);
}

/**
 * Call before older history is added above the content; the returned function, called once it has rendered, keeps the
 * text under the reader where it was. The held blank space is carried down with the content, so it is not what the new
 * rows fill.
 */
export function anchorPrepend(scroller: Scroller): () => void {
	const top = scroller.scrollTop;
	const end = contentEnd(scroller);
	const blank = scroller.scrollHeight - end;
	return () => {
		floors.get(scroller)?.hold(blank);
		scroller.scrollTop = top + contentEnd(scroller) - end;
	};
}

export function holdTail(node: HTMLElement, params: { key: string; live: boolean }) {
	const scroller = node.parentElement;
	let current = params.key;
	let live = false;
	let holding = false;
	let high = 0;
	let width = 0;
	let pad = 0;
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
	const bandPad = () => (scroller ? parseFloat(getComputedStyle(scroller).paddingBottom) || 0 : 0);
	/**
	 * The band under the content (the composer's room) got shorter while the view reaches into the blank below the
	 * content: the view comes down with it, as a browser's clamp would have, and no further than the content end.
	 */
	const shedBand = () => {
		if (!scroller) return;
		const now = bandPad();
		const by = pad - now;
		pad = now;
		if (by <= 0) return;
		const end = Math.max(0, endTop() + now - scroller.clientHeight);
		if (scroller.scrollTop > end) scroller.scrollTop = Math.max(end, scroller.scrollTop - by);
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
			pad = bandPad();
			// A different width reflows the text, so the old height means nothing: start again from the new one.
			resizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
				shedBand();
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

	if (scroller) {
		floors.set(scroller, {
			end: () => {
				if (node.offsetHeight === 0) return scroller.scrollHeight;
				return Math.min(scroller.scrollHeight, endTop() + bandPad());
			},
			hold: (blank) => {
				if (blank <= 0) return;
				high = endTop() + blank;
				place(high);
			},
		});
	}
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
		destroy() {
			stop();
			if (scroller) floors.delete(scroller);
		},
	};
}
