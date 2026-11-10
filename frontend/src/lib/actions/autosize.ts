import { scroller } from './keepInView';

/** The field's value, or the value and `fit`: grow until what scrolls the field (a popover, a sheet) would have to scroll, then scroll. */
type AutosizeParam = string | undefined | { value: string; fit?: boolean };

/**
 * Makes a textarea as tall as its text, so a long line wraps and shows instead of scrolling sideways. The height is
 * written as an explicit pixel value, which the field's own CSS transition eases (motion.md: the input box gains or
 * loses a line). The field's CSS `max-height` caps it; past the cap it scrolls. With `fit`, a field that has no
 * `max-height` grows only until its scroller is full: it takes the room left once everything else in that scroller
 * shows, in whole lines and never less than six, and scrolls past that. Pass the field's value so the action re-fits
 * whenever it changes, including when it is cleared after a send. Write `use:autosize` after `bind:value`: effects run in
 * attribute order, and a re-fit that runs before the bound value is written measures the old text.
 */
export function autosize(node: HTMLTextAreaElement, param?: AutosizeParam) {
	let current = node.offsetHeight;
	let fit = typeof param === 'object' && !!param.fit;
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
		const started = scrolls && node.style.overflowY !== 'auto';
		node.style.overflowY = scrolls ? 'auto' : 'hidden';
		current = Number.isFinite(cap) ? Math.min(wanted, cap) : wanted;
		node.style.height = `${current}px`;
		// Text that took the field past its cap in one go (a paste) was held at the top while the field fitted it; the caret at
		// the end is brought back into view.
		if (started && node === document.activeElement && node.selectionEnd === node.value.length) node.scrollTop = node.scrollHeight;
		hold();
	};
	// With the field at its full height, `over` is how far its scroller overflows: the field gives up that much, in whole lines.
	const room = (style: CSSStyleDeclaration, wanted: number, inset: number) => {
		const owner = scroller(node);
		const over = owner ? owner.scrollHeight - owner.clientHeight : 0;
		if (over <= 1) return Number.POSITIVE_INFINITY;
		const line = Number.parseFloat(style.lineHeight);
		if (!(line > 0)) return Math.max(wanted - over, 0);
		const lines = Math.max(6, Math.floor((wanted - over - inset + 0.5) / line));
		return lines * line + inset;
	};
	// Below the cap the field holds all its text: the caret's line scrolling into view while the height still eases would lift the
	// text one line for a frame, so the scroll is put back before it is drawn.
	const hold = () => { if (node.style.overflowY === 'hidden' && node.scrollTop !== 0) node.scrollTop = 0; };
	node.addEventListener('scroll', hold);
	resize();
	return {
		update: (next?: AutosizeParam) => { fit = typeof next === 'object' && !!next.fit; resize(); },
		destroy: () => node.removeEventListener('scroll', hold),
	};
}
