/** Keeps one viewport observer per rendered unread reply across comment projections. */
export function createReplyVisibility(onVisible: (id: string) => void, onHidden: (id: string) => void, onClear: () => void) {
	const observers = new Map<string, { element: HTMLElement; observer: IntersectionObserver }>();
	function clear() {
		for (const { observer } of observers.values()) observer.disconnect();
		observers.clear();
		onClear();
	}
	function sync(root: HTMLElement | undefined, active: boolean, unreadIds: string[]) {
		if (!root || !active || typeof IntersectionObserver === 'undefined') { clear(); return; }
		const unread = new Set(unreadIds);
		const elements = new Map(Array.from(root.querySelectorAll<HTMLElement>('[data-reply-id]'))
			.filter((element) => element.dataset.replyId && unread.has(element.dataset.replyId))
			.map((element) => [element.dataset.replyId!, element]));
		for (const [id, existing] of observers) {
			if (elements.get(id) !== existing.element) {
				existing.observer.disconnect(); observers.delete(id); onHidden(id);
			}
		}
		for (const [id, element] of elements) {
			if (observers.has(id)) continue;
			const observer = new IntersectionObserver((entries) => {
				const entry = entries.at(-1);
				const fillsRoot = entry?.rootBounds && entry.rootBounds.height > 0 && entry.intersectionRect.height >= entry.rootBounds.height - 1;
				if (entry?.isIntersecting && (entry.intersectionRatio === 1 || fillsRoot)) onVisible(id);
				else onHidden(id);
			}, { threshold: Array.from({ length: 21 }, (_, index) => index / 20) });
			observers.set(id, { element, observer });
			observer.observe(element);
		}
	}
	return { clear, sync };
}
