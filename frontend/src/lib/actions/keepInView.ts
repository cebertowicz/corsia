/** The nearest ancestor that scrolls on its own, or null when the page itself is the scroller. */
export function scroller(node: HTMLElement): HTMLElement | null {
	for (let parent = node.parentElement; parent; parent = parent.parentElement) {
		if (/auto|scroll/.test(getComputedStyle(parent).overflowY)) return parent;
	}
	return null;
}

/** The block's own scroll margins: `scrollIntoView` leaves them clear, so they spend from the scroller's room. */
function scrollMargins(element: HTMLElement): number {
	const style = getComputedStyle(element);
	return (parseFloat(style.scrollMarginTop) || 0) + (parseFloat(style.scrollMarginBottom) || 0);
}

/** The scroller's own room: its scroll padding (a band something floats over) is left clear by every scroll into view, the browser's caret reveal included. */
function roomOf(owner: HTMLElement): number {
	const style = getComputedStyle(owner);
	return owner.clientHeight - (parseFloat(style.scrollPaddingTop) || 0) - (parseFloat(style.scrollPaddingBottom) || 0);
}

/**
 * What sticky `[data-pin-bottom]` rows (a suggestion's Accept and Reject) cover at the scroller's bottom edge: room the scroller has but a revealed field must not land under.
 * A sticky row sticks at the edge of the scroller's content box, so the scroller's own bottom padding sits under the row's own height.
 */
function pinnedBottom(owner: HTMLElement | null): number {
	if (!owner) return 0;
	let covered = 0;
	for (const row of owner.querySelectorAll<HTMLElement>('[data-pin-bottom]')) {
		if (getComputedStyle(row).position === 'sticky') covered += row.offsetHeight;
	}
	return covered ? covered + (parseFloat(getComputedStyle(owner).paddingBottom) || 0) : 0;
}

/**
 * Keeps `node` scrolled into its nearest scrolling ancestor while `enabled`:
 * once on mount and again whenever the visual viewport or that scroller
 * resizes (an on-screen keyboard, a shortened window, a sheet re-measuring
 * itself), so a field the person is typing into never ends up below the
 * visible edge of a sheet that owns its own scroller.
 *
 * `whole` is the block `node` belongs to. While that block, including the scroll
 * margins it asks a scroll to leave clear, fits the scroller it is revealed
 * instead of the node, so bringing a field into view never pushes the top of the
 * block it sits at the end of out of sight above and never clips the ring drawn
 * around that block; a block too tall for the scroller keeps the node-into-view
 * behaviour, and a node itself taller than the scroller shows its end.
 *
 * `clearAfter` is how much a reveal leaves clear below the node (a line that belongs under a field). It is the node's own
 * scroll margin, given up as far as the node needs the room: a node that fits the scroller on its own is shown whole, and
 * only what is left over after it stays clear; a node taller than the room keeps the whole margin, so its end and what sits under it show.
 *
 * A sticky row pinned to the scroller's bottom (`[data-pin-bottom]`) is room the field must not land under: while the node is revealed alone the
 * scroller carries that row's height as scroll padding, so the browser's own caret reveal after each typed character leaves it clear too, and
 * a reveal that still ends under the row (the browser does not always honour the padding for a field taller than the room above the row)
 * scrolls the scroller by what is left. A revealed block holds that row at its own end, so it needs none.
 *
 * A resize brings the node back while its end was in view, and always while the node has focus: a reader who scrolled away
 * from an unfocused field (up to read the thread above a long reply) is not pulled back to it when the scroller or viewport
 * changes size, but a field the person is typing into is. A scroll the browser makes on its own (after a sheet's last shrink,
 * say) can read the field as below the end, so for a focused field that reading never stops the resize from bringing it back.
 * The scroll a reveal makes is not the reader's: a sheet still shrinking can leave the field below its end by the time that
 * scroll's event arrives.
 *
 * Typing at the end of a field brings its end back as growing does: the browser's own caret reveal stops at the caret
 * line, leaving the field's padding and the Send beside it past the scroller's end when the text did not wrap.
 *
 * A scroller the person reads in (marked `data-reading-scroller`: the chat transcript, which a margin card's field sits
 * in) is never scrolled for its own or the viewport's change of size: that would move the text under the reader. There
 * the field is brought back only for its own growth, for typing at its end, and for the on-screen keyboard coming up
 * over it: while the field has focus and the viewport got shorter, a field now outside the scroller's visible band is
 * scrolled back by the least that shows it. The person tapped to type, so that move is theirs.
 */
export function keepInView(node: HTMLElement, options: { enabled: boolean; whole?: HTMLElement; clearAfter?: number }) {
	let { enabled, whole, clearAfter } = options;
	let frame = 0;
	const owner = scroller(node);
	const reading = owner?.matches('[data-reading-scroller]') ?? false;
	/** Whether the node's end (the caret of a growing field, and its Send) was in view at the last scroll. */
	let endShown = true;
	/** The bottom scroll padding this action added to the scroller for a pinned row. */
	let held = 0;
	/** Where this action's own last reveal left the scroller. */
	let revealedTop: number | null = null;
	const topOf = () => owner?.scrollTop ?? window.scrollY;
	const readShown = () => {
		if (topOf() === revealedTop) return;
		revealedTop = null;
		const end = node.getBoundingClientRect().bottom;
		const view = owner?.getBoundingClientRect() ?? { top: 0, bottom: window.visualViewport?.height ?? window.innerHeight };
		endShown = end >= view.top && end <= view.bottom - held + 1;
	};
	const reveal = () => {
		if (!enabled) return;
		const room = owner ? roomOf(owner) + held : window.visualViewport?.height ?? window.innerHeight;
		const fits = whole !== undefined && owner !== null && whole.getBoundingClientRect().height + scrollMargins(whole) <= room;
		const target = fits ? whole! : node;
		const pinned = owner && !fits ? pinnedBottom(owner) : 0;
		if (owner && pinned !== held) {
			const own = (parseFloat(getComputedStyle(owner).scrollPaddingBottom) || 0) - held;
			owner.style.scrollPaddingBottom = `${own + pinned}px`;
			held = pinned;
		}
		if (clearAfter !== undefined) {
			const spare = room - pinned - node.getBoundingClientRect().height;
			node.style.scrollMarginBlockEnd = `${spare >= 0 ? Math.min(clearAfter, Math.floor(spare)) : clearAfter}px`;
		}
		// A node taller than the room shows its end, where the caret of a growing field and its Send are.
		const block = !fits && node.getBoundingClientRect().height + scrollMargins(node) > room - pinned ? 'end' : 'nearest';
		if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block });
		if (owner && pinned && target === node) {
			// The box's rectangle includes its border, which is not scrollable room.
			const edge = owner.getBoundingClientRect().bottom - (parseFloat(getComputedStyle(owner).borderBottomWidth) || 0);
			const over = node.getBoundingClientRect().bottom - (edge - pinned);
			if (over > 1) owner.scrollTop += over;
		}
		endShown = true;
		revealedTop = topOf();
	};
	const schedule = () => {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(reveal);
	};
	const resized = () => { if (endShown || document.activeElement === node) schedule(); };
	const typed = () => {
		if (node instanceof HTMLTextAreaElement || node instanceof HTMLInputElement) {
			if (node.selectionEnd === node.value.length) schedule();
		}
	};
	const viewport = reading ? null : window.visualViewport;
	viewport?.addEventListener('resize', resized);
	/** The viewport's height at the last reading: a reading scroller tells the keyboard coming up from other resizes by it. */
	const viewportHeight = () => window.visualViewport?.height ?? window.innerHeight;
	let lastViewport = viewportHeight();
	let keyFrame = 0;
	/** Scrolls a reading scroller by the least that brings the node inside its visible band: the scroller's box less its scroll padding, cut to the visual viewport. */
	const uncover = () => {
		if (!owner || document.activeElement !== node) return;
		const style = getComputedStyle(owner), box = owner.getBoundingClientRect(), vv = window.visualViewport;
		const vTop = vv?.offsetTop ?? 0, vBottom = vTop + (vv?.height ?? window.innerHeight);
		const top = Math.max(box.top + (parseFloat(style.scrollPaddingTop) || 0), vTop);
		const bottom = Math.min(box.bottom - (parseFloat(style.scrollPaddingBottom) || 0), vBottom);
		const rect = node.getBoundingClientRect();
		const shift = rect.bottom > bottom ? rect.bottom - bottom : rect.top < top ? rect.top - top : 0;
		if (shift) owner.scrollTop += shift;
	};
	const keyboard = () => {
		const now = viewportHeight(), shorter = now < lastViewport;
		lastViewport = now;
		if (!shorter || document.activeElement !== node) return;
		cancelAnimationFrame(keyFrame);
		keyFrame = requestAnimationFrame(uncover);
	};
	if (reading) {
		window.visualViewport?.addEventListener('resize', keyboard);
		window.addEventListener('resize', keyboard);
	}
	// The node growing is the person typing into it; the scroller or viewport changing size is not.
	const grown = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
	const ownerResized = !reading && typeof ResizeObserver === 'function' ? new ResizeObserver(resized) : null;
	if (owner) ownerResized?.observe(owner);
	grown?.observe(node);
	node.addEventListener('input', typed);
	(owner ?? window).addEventListener('scroll', readShown, { passive: true });
	queueMicrotask(reveal);

	return {
		update(next: { enabled: boolean; whole?: HTMLElement; clearAfter?: number }) {
			({ enabled, whole, clearAfter } = next);
			reveal();
		},
		destroy() {
			cancelAnimationFrame(frame);
			cancelAnimationFrame(keyFrame);
			if (owner && held) owner.style.scrollPaddingBottom = '';
			viewport?.removeEventListener('resize', resized);
			if (reading) {
				window.visualViewport?.removeEventListener('resize', keyboard);
				window.removeEventListener('resize', keyboard);
			}
			node.removeEventListener('input', typed);
			(owner ?? window).removeEventListener('scroll', readShown);
			grown?.disconnect();
			ownerResized?.disconnect();
		},
	};
}
