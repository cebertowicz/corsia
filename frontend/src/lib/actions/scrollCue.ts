/**
 * Marks a scroller `data-more` while content sits below its visible edge and `data-above` while content sits above
 * it, so its owner's CSS can fade each edge only on the side where there is more to scroll to.
 */
export function scrollCue(node: HTMLElement) {
	const sync = () => {
		node.dataset.more = String(node.scrollTop + node.clientHeight < node.scrollHeight - 1);
		node.dataset.above = String(node.scrollTop > 1);
	};
	node.addEventListener('scroll', sync, { passive: true });
	const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(sync);
	resize?.observe(node);
	const mutate = typeof MutationObserver === 'undefined' ? null : new MutationObserver(sync);
	mutate?.observe(node, { childList: true, subtree: true });
	sync();
	return { destroy() { node.removeEventListener('scroll', sync); resize?.disconnect(); mutate?.disconnect(); } };
}
