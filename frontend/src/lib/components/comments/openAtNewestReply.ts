import { scroller } from '$lib/actions/keepInView';

/**
 * A thread opened on its own (a popover, the sheet's thread) opens at its newest reply: what scrolls it starts at the
 * card's end, so the reply field shows under the newest reply when both fit, and never past the newest reply's first line.
 * Runs once per card, two frames on, since a popover takes its place and height in the frame after it opens.
 */
const opened = new WeakSet<HTMLElement>();
export function openAtNewestReply(card: HTMLElement, reply: HTMLElement) {
	if (opened.has(card)) return;
	opened.add(card);
	requestAnimationFrame(() => requestAnimationFrame(() => {
		const port = scroller(card);
		if (!port || !reply.isConnected) return;
		const toEnd = port.scrollHeight - port.clientHeight - port.scrollTop;
		const toReply = reply.getBoundingClientRect().top - port.getBoundingClientRect().top - (Number.parseFloat(getComputedStyle(port).paddingTop) || 0);
		port.scrollTop += Math.max(0, Math.min(toEnd, toReply));
	}));
}
