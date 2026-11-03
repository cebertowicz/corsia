/**
 * Keeps the live row of a turn from getting shorter while the turn is live. The row swaps between reasoning text, tool
 * steps and the quiet-gap dots, which are different heights; each shortening would pull the end of the scroller up
 * under a reader at the end, and the next growth would pin them back down. The node's own `min-height` ratchets up to
 * the tallest its content (its first child) has been, so the room is real content and the scroller's height stays what
 * the browser measures.
 *
 * `ratchet` is false while the row shows the streamed reply: that text is not a state to hold, it becomes a message
 * above the node, so the room it takes is already spoken for. When it clears, the node moves down by that message's
 * height and gives up the same height of room at once, so the end of the scroller stays where it was. `hold` going
 * false (the turn ended) gives back what is left once, as a glide on the drawer's motion tokens, so the eye can follow
 * the line it was on; under reduced motion the global rule makes it instant. A reply as tall as the room leaves
 * nothing to give back. It never runs while the turn is live, and a new turn that starts mid-glide keeps the height
 * the glide had reached.
 *
 * In Focus the room sits inside an open current-work block that turns into the finished block when the turn ends, so
 * this node also gives back what that took away: `before` is where the node sat just before the DOM changes of the
 * update that releases it, and the amount it has moved up since is added to the room.
 */
const GLIDE = 'min-height var(--dur-drawer) var(--ease-ios-sheet)';

export function holdHeight(node: HTMLElement, params: { hold: boolean; ratchet: boolean; before?: number }) {
	let current = params;
	let peak = 0;
	let top = 0;
	let gliding: ReturnType<typeof setTimeout> | null = null;
	const content = node.firstElementChild;
	const finish = () => {
		if (gliding) clearTimeout(gliding);
		gliding = null;
		node.style.transition = '';
		node.style.minHeight = '';
	};
	const observer = !content || typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
		if (!current.hold || !current.ratchet) return;
		const height = content.getBoundingClientRect().height;
		if (height <= peak) return;
		peak = height;
		node.style.minHeight = `${height}px`;
	});
	if (content) observer?.observe(content);
	return {
		update(next: { hold: boolean; ratchet: boolean; before?: number }) {
			if (!current.hold && next.hold && gliding) {
				// A new turn while the last glide runs: keep the height it has reached, so nothing jumps.
				const reached = node.getBoundingClientRect().height;
				finish();
				peak = reached;
				node.style.minHeight = `${reached}px`;
			}
			if (current.hold && next.hold && current.ratchet !== next.ratchet) {
				if (!next.ratchet) {
					top = node.offsetTop;
				} else {
					// The reply cleared and landed above the node: give up that much room, so nothing under the reader moves.
					peak = Math.max(0, peak - (node.offsetTop - top));
					node.style.minHeight = `${peak}px`;
				}
			}
			if (current.hold && !next.hold) {
				const room = peak + (next.before === undefined ? 0 : Math.max(0, next.before - node.offsetTop));
				peak = 0;
				if (room > 0) {
					node.style.transition = 'none';
					node.style.minHeight = `${room}px`;
					void node.offsetHeight;
					node.style.transition = GLIDE;
					node.style.minHeight = '0px';
					gliding = setTimeout(finish, 500);
				} else {
					node.style.minHeight = '';
				}
			}
			current = next;
		},
		destroy() {
			if (gliding) clearTimeout(gliding);
			observer?.disconnect();
		},
	};
}
