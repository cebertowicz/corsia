const STEPS = 20;
export const replyVisibilityThresholds = Array.from({ length: STEPS + 1 }, (_, index) => index / STEPS);

/** The viewport height left after every clipping container around the reply, as the observer clips it. */
function visibleHeight(element: HTMLElement, viewport: DOMRectReadOnly) {
	let top = viewport.top;
	let bottom = viewport.top + viewport.height;
	for (let parent = element.parentElement; parent; parent = parent.parentElement) {
		if (!/auto|scroll|overlay|hidden|clip/.test(getComputedStyle(parent).overflowY)) continue;
		const rect = parent.getBoundingClientRect();
		top = Math.max(top, rect.top);
		bottom = Math.min(bottom, rect.bottom);
	}
	return bottom - top;
}

/**
 * A reply is on screen when fully visible, or when it is taller than the area and its visible part fills it.
 * A filling reply's ratio stops changing, so the last callback can fall up to one threshold step short of full.
 */
export function onScreen(entry: IntersectionObserverEntry, element: HTMLElement) {
	if (entry.intersectionRatio === 1) return true;
	const area = entry.rootBounds ? visibleHeight(element, entry.rootBounds) : 0;
	const height = entry.boundingClientRect.height;
	return area > 0 && height > area && entry.intersectionRect.height >= area - height / STEPS - 1;
}

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
			let visible: boolean | undefined;
			const observer = new IntersectionObserver((entries) => {
				const entry = entries.at(-1);
				const next = !!entry?.isIntersecting && onScreen(entry, element);
				if (visible === next) return;
				visible = next;
				if (next) onVisible(id); else onHidden(id);
			}, { threshold: replyVisibilityThresholds });
			observers.set(id, { element, observer });
			observer.observe(element);
		}
	}
	return { clear, sync };
}
