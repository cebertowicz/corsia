import { scroller } from './keepInView';

/**
 * Makes a textarea as tall as its text, so a long line wraps and shows instead of scrolling sideways. The height is
 * written as an explicit pixel value, which the field's own CSS transition eases (motion.md: the input box gains or
 * loses a line). The field's CSS `max-height` caps it; past the cap it scrolls. A field with no `max-height` (a comment
 * reply) grows with its text and never scrolls itself: what holds it (a popover, a sheet) scrolls instead. Pass the
 * field's value so the action re-fits whenever it changes, including when it is cleared after a send. Write
 * `use:autosize` after `bind:value`: effects run in attribute order, and a re-fit that runs before the bound value is
 * written measures the old text.
 */
export function autosize(node: HTMLTextAreaElement, _value?: string) {
	let current = node.offsetHeight;
	const owner = scroller(node);
	const resize = () => {
		const style = getComputedStyle(node);
		const padding = (Number.parseFloat(style.paddingTop) || 0) + (Number.parseFloat(style.paddingBottom) || 0);
		const border = (Number.parseFloat(style.borderTopWidth) || 0) + (Number.parseFloat(style.borderBottomWidth) || 0);
		const borderBox = style.boxSizing === 'border-box';
		// Measure with the transition off and the height collapsed, then put the old height back before easing to the new one.
		// The collapse shortens what holds the field for that one layout, which clamps its scroll offset (to the top when the
		// rest fits), and a refit to the same height gives no resize for anything to reveal from, so the offset is put back.
		const port = owner ?? document.scrollingElement;
		const top = port?.scrollTop ?? 0;
		node.style.transition = 'none';
		node.style.height = '0px';
		const wanted = node.scrollHeight + (borderBox ? border : -padding);
		const cap = Number.parseFloat(style.maxHeight);
		node.style.height = `${current}px`;
		void node.offsetHeight;
		if (port && port.scrollTop !== top) port.scrollTop = top;
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
	// Below the cap the field holds all its text: the caret's line scrolling into view while the height still eases would lift the
	// text one line for a frame, so the scroll is put back before it is drawn.
	const hold = () => { if (node.style.overflowY === 'hidden' && node.scrollTop !== 0) node.scrollTop = 0; };
	node.addEventListener('scroll', hold);
	resize();
	// Refit when the field's width changes (a drawer resized around a keyboard or a drag rewraps its text). A refit
	// measures with the transition off, which cancels a growth ease still running on the field, so it waits for the
	// ease instead of aborting it mid-flight.
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
		update: (_next?: string) => resize(),
		destroy: () => {
			cancelAnimationFrame(frame); observer?.disconnect();
			node.removeEventListener('scroll', hold);
		},
	};
}
