/**
 * Makes a textarea as tall as its text, so a long line wraps and shows instead of scrolling sideways. The height is
 * written as an explicit pixel value, which the field's own CSS transition eases (motion.md: the input box gains or
 * loses a line). The field's CSS `max-height` caps it; past the cap it scrolls. Pass the field's value so the action
 * re-fits whenever it changes, including when it is cleared after a send.
 */
export function autosize(node: HTMLTextAreaElement, _value?: string) {
	let current = node.offsetHeight;
	const fit = () => {
		const style = getComputedStyle(node);
		const padding = (Number.parseFloat(style.paddingTop) || 0) + (Number.parseFloat(style.paddingBottom) || 0);
		const border = (Number.parseFloat(style.borderTopWidth) || 0) + (Number.parseFloat(style.borderBottomWidth) || 0);
		// Measure with the transition off and the height collapsed, then put the old height back before easing to the new one.
		node.style.transition = 'none';
		node.style.height = '0px';
		const wanted = node.scrollHeight + (style.boxSizing === 'border-box' ? border : -padding);
		node.style.height = `${current}px`;
		void node.offsetHeight;
		node.style.transition = '';
		const cap = Number.parseFloat(style.maxHeight);
		node.style.overflowY = Number.isFinite(cap) && wanted > cap ? 'auto' : 'hidden';
		current = Number.isFinite(cap) ? Math.min(wanted, cap) : wanted;
		node.style.height = `${current}px`;
		hold();
	};
	// Below the cap the field holds all its text: the caret's line scrolling into view while the height still eases would lift the
	// text one line for a frame, so the scroll is put back before it is drawn.
	const hold = () => { if (node.style.overflowY === 'hidden' && node.scrollTop !== 0) node.scrollTop = 0; };
	node.addEventListener('scroll', hold);
	fit();
	return { update: fit, destroy: () => node.removeEventListener('scroll', hold) };
}
