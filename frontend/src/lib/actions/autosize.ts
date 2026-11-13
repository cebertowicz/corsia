import { scroller } from './keepInView';

/** The field's value, or the value and `fit`: grow until what scrolls the field (a popover, a sheet) would have to scroll, then scroll. */
type AutosizeParam = string | undefined | { value: string; fit?: boolean };

/**
 * Makes a textarea as tall as its text, so a long line wraps and shows instead of scrolling sideways. The height is
 * written as an explicit pixel value, which the field's own CSS transition eases (motion.md: the input box gains or
 * loses a line). The field's CSS `max-height` caps it; past the cap it scrolls. With `fit`, a field that has no
 * `max-height` grows only until its scroller is full: it takes the room left once everything else in that scroller
 * shows, in whole lines and never less than one, and scrolls past that. Pass the field's value so the action re-fits
 * whenever it changes, including when it is cleared after a send. Write `use:autosize` after `bind:value`: effects run in
 * attribute order, and a re-fit that runs before the bound value is written measures the old text.
 */
export function autosize(node: HTMLTextAreaElement, param?: AutosizeParam) {
	let current = node.offsetHeight;
	let fit = typeof param === 'object' && !!param.fit;
	let lastBoundedReply: HTMLElement | null = null;
	const resize = () => {
		const style = getComputedStyle(node);
		const padding = (Number.parseFloat(style.paddingTop) || 0) + (Number.parseFloat(style.paddingBottom) || 0);
		const border = (Number.parseFloat(style.borderTopWidth) || 0) + (Number.parseFloat(style.borderBottomWidth) || 0);
		const borderBox = style.boxSizing === 'border-box';
		// Measure with the transition off and the height collapsed, then put the old height back before easing to the new one.
		node.style.transition = 'none';
		node.style.height = '0px';
		const wanted = node.scrollHeight + (borderBox ? border : -padding);
		let cap = Number.parseFloat(style.maxHeight);
		if (fit && !Number.isFinite(cap)) {
			node.style.height = `${wanted}px`;
			cap = room(style, wanted, borderBox ? border + padding : 0);
		}
		node.style.height = `${current}px`;
		void node.offsetHeight;
		node.style.transition = '';
		// scrollHeight is a rounded integer and a cap of five `lh` is fractional (125.953px for a 126px five-line field), so a field
		// holding exactly its cap's worth of text reads a subpixel over it. A pixel of tolerance keeps that field "fitting", so it
		// stays hidden and held instead of turning scrollable for one frame.
		const scrolls = Number.isFinite(cap) && wanted > cap + 1;
		node.style.overflowY = scrolls ? 'auto' : 'hidden';
		current = Number.isFinite(cap) ? Math.min(wanted, cap) : wanted;
		node.style.height = `${current}px`;
		// Refitting can put a pasted ending back at the top even when the field already scrolled.
		if (scrolls && node === document.activeElement && node.selectionEnd === node.value.length) node.scrollTop = node.scrollHeight;
		hold();
	};
	// With the field at its full height, `over` is how far its scroller overflows: the field gives up that much, in whole lines.
	const room = (style: CSSStyleDeclaration, wanted: number, inset: number) => {
		const owner = scroller(node);
		const pinned = node.closest<HTMLElement>('[data-autosize-sticky]');
		const card = node.closest<HTMLElement>('[data-autosize-room]') ?? (pinned && getComputedStyle(pinned).position === 'sticky' ? pinned : null);
		// Fit the thread or margin card in its view, reserving the discussion and its controls.
		const viewHeight = card ? Number.parseFloat(getComputedStyle(card).getPropertyValue('--autosize-room')) : NaN;
		// clientHeight includes padding, which belongs outside the card's available space.
		const ownerStyle = owner && card && !Number.isFinite(viewHeight) ? getComputedStyle(owner) : null;
		const ownerPadding = ownerStyle ? (Number.parseFloat(ownerStyle.paddingTop) || 0) + (Number.parseFloat(ownerStyle.paddingBottom) || 0) : 0;
		const original = card?.querySelector<HTMLElement>('[data-discussion-original]');
		const replies = card?.querySelector<HTMLElement>('[data-discussion-replies]');
		// Release the previous bound, then measure the view and the card: a panel sized by its content would otherwise
		// report the room the last bound left, so every refit would take a little more of it.
		const oldOriginal = original?.scrollTop ?? 0, oldReplies = replies?.scrollTop ?? 0;
		const alreadyBounded = !!replies?.style.maxHeight;
		if (original) original.style.maxHeight = '';
		if (replies) replies.style.maxHeight = '';
		const height = owner && card ? (Number.isFinite(viewHeight) ? viewHeight : owner.clientHeight - ownerPadding) : null;
		if (height !== null && original) {
			const originalHeight = original.offsetHeight, repliesHeight = replies?.offsetHeight ?? 0;
			const line = Number.parseFloat(style.lineHeight) || 0;
			const originalLine = original.querySelector<HTMLElement>('.ct-markdown');
			const latest = replies?.querySelector<HTMLElement>('.ct-reply:last-child') ?? null;
			const replyText = latest?.querySelector<HTMLElement>('.ct-markdown');
			const originalMinimum = originalLine ? Number.parseFloat(getComputedStyle(originalLine).lineHeight) || 0 : 0;
			const replyMinimum = latest && replyText ? replyText.getBoundingClientRect().top - latest.getBoundingClientRect().top + (Number.parseFloat(getComputedStyle(replyText).lineHeight) || 0) : 0;
			const room = height - (card!.offsetHeight - wanted - originalHeight - repliesHeight);
			// Give the draft a third line when the original and latest reply can still each show a line.
			const fieldRoom = room >= originalMinimum + replyMinimum + 3 * line + inset ? 3 * line + inset : 2 * line + inset;
			const budget = room - fieldRoom;
			if (budget < originalHeight + repliesHeight) {
				const originalRoom = Math.min(originalHeight, Math.max(originalMinimum, replies ? Math.min(budget / 2, budget - replyMinimum) : budget));
				const replyRoom = Math.min(repliesHeight, Math.max(replyMinimum, budget - originalRoom));
				original.style.maxHeight = `${originalRoom}px`; if (replies) replies.style.maxHeight = `${replyRoom}px`;
				for (const [region, bounded] of [[original, originalRoom < originalHeight], [replies, replyRoom < repliesHeight]] as const) {
					if (!region) continue;
					if (bounded) region.setAttribute('tabindex', '0'); else region.removeAttribute('tabindex');
				}
				original.scrollTop = oldOriginal;
				if (replies && latest && (!alreadyBounded || latest !== lastBoundedReply)) replies.scrollTop += latest.getBoundingClientRect().top - replies.getBoundingClientRect().top;
				else if (replies) replies.scrollTop = oldReplies;
				lastBoundedReply = latest;
			} else {
				lastBoundedReply = null;
				original.removeAttribute('tabindex'); replies?.removeAttribute('tabindex');
			}
		}
		const available = height !== null && card ? height - (card.offsetHeight - wanted) : null;
		const over = owner ? owner.scrollHeight - owner.clientHeight : 0;
		if (available === null && over <= 1) return Number.POSITIVE_INFINITY;
		const room = available ?? wanted - over;
		const line = Number.parseFloat(style.lineHeight);
		if (!(line > 0)) return Math.max(room, 0);
		const minimum = pinned && card !== pinned ? 2 : 1;
		const lines = Math.max(minimum, Math.floor((room - inset + 0.5) / line));
		return lines * line + inset;
	};
	// Below the cap the field holds all its text: the caret's line scrolling into view while the height still eases would lift the
	// text one line for a frame, so the scroll is put back before it is drawn.
	const hold = () => { if (node.style.overflowY === 'hidden' && node.scrollTop !== 0) node.scrollTop = 0; };
	node.addEventListener('scroll', hold);
	resize();
	// Refit when the available view changes, including a drawer changing size around a keyboard or a drag. A refit
	// measures with the transition off, which cancels a growth ease still running on the field, so it waits for the
	// ease instead of aborting it mid-flight.
	const owner = scroller(node);
	let width = owner?.clientWidth, height = owner?.clientHeight, frame = 0;
	const refit = () => {
		cancelAnimationFrame(frame);
		// Check and write in the same frame: measuring turns the transition off, so an ease started between the
		// check and the write would be aborted mid-flight.
		frame = requestAnimationFrame(() => {
			// Only a still-running ease may hold the refit: a finished animation stays listed for a frame, and
			// waiting on its already-resolved `finished` would defer forever without ever writing the height.
			const running = node.getAnimations().filter((animation) => animation.playState === 'running');
			if (running.length) {
				// Waiting on the ease is enough on its own: a transition replaced before it completes rejects
				// `finished` rather than firing `transitionend`, and both settle the wait.
				void Promise.allSettled(running.map((animation) => animation.finished)).then(() => refit());
				return;
			}
			resize();
		});
	};
	const observer = owner && typeof ResizeObserver === 'function' ? new ResizeObserver(() => {
		if (owner.clientWidth === width && owner.clientHeight === height) return;
		width = owner.clientWidth; height = owner.clientHeight;
		refit();
	}) : null;
	if (owner) observer?.observe(owner);
	return {
		update: (next?: AutosizeParam) => { fit = typeof next === 'object' && !!next.fit; resize(); },
		destroy: () => {
			cancelAnimationFrame(frame); observer?.disconnect();
			node.removeEventListener('scroll', hold);
			const card = node.closest<HTMLElement>('[data-autosize-room]');
			card?.querySelectorAll<HTMLElement>('[data-discussion-original], [data-discussion-replies]').forEach(region => {
				region.style.maxHeight = ''; region.removeAttribute('tabindex');
			});
		},
	};
}
