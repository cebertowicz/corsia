import { scroller } from '$lib/actions/keepInView';

/**
 * A thread opened on its own (a popover, the sheet's thread) opens at its newest reply: what scrolls it starts at the
 * card's end, so the reply field shows under the newest reply when both fit, and never past the newest reply's first line.
 * Runs once per open of the card, two frames on, since a popover takes its place and height in the frame after it opens.
 * `open` is the surface's open count (`surfaceOpen`): a sheet or popover opened again while it is still closing keeps the
 * same card, and that reopen is a new open too. A re-render or a new reply within one open does not scroll again.
 */
const opened = new WeakMap<HTMLElement, number>();
export function openAtNewestReply(card: HTMLElement, reply: HTMLElement, open = 0) {
	if (opened.get(card) === open) return;
	opened.set(card, open);
	requestAnimationFrame(() => requestAnimationFrame(() => {
		const port = scroller(card);
		if (!port || !reply.isConnected) return;
		const toEnd = port.scrollHeight - port.clientHeight - port.scrollTop;
		const toReply = reply.getBoundingClientRect().top - port.getBoundingClientRect().top - (Number.parseFloat(getComputedStyle(port).paddingTop) || 0);
		port.scrollTop += Math.max(0, Math.min(toEnd, toReply));
	}));
}
