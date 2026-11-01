/** Keeps a box that scrolls inside itself showing its newest content: scrolls to the end now and whenever `content` changes. */
export function autoScroll(node: HTMLElement, _content: unknown) {
	node.scrollTop = node.scrollHeight;
	return {
		update() { node.scrollTop = node.scrollHeight; },
	};
}
