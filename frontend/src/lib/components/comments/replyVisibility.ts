/** The viewport narrowed by every scroll container around the reply. */
function visibleArea(element: HTMLElement, viewport: DOMRectReadOnly) {
	let top = viewport.top;
	let bottom = viewport.top + viewport.height;
	for (let parent = element.parentElement; parent; parent = parent.parentElement) {
		if (!/auto|scroll|overlay/.test(getComputedStyle(parent).overflowY)) continue;
		const rect = parent.getBoundingClientRect();
		top = Math.max(top, rect.top);
		bottom = Math.min(bottom, rect.bottom);
	}
	return { top, bottom, height: bottom - top };
}

function fillsArea(visibleHeight: number, areaHeight: number) {
	return areaHeight > 0 && visibleHeight >= areaHeight - 1;
}

/** Keeps one viewport observer per rendered unread reply across comment projections. */
export function createReplyVisibility(onVisible: (id: string) => void, onHidden: (id: string) => void, onClear: () => void) {
	const observers = new Map<string, { element: HTMLElement; observer: IntersectionObserver; stop: () => void }>();
	function clear() {
		for (const { observer, stop } of observers.values()) { observer.disconnect(); stop(); }
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
				existing.observer.disconnect(); existing.stop(); observers.delete(id); onHidden(id);
			}
		}
		for (const [id, element] of elements) {
			if (observers.has(id)) continue;
			let visible: boolean | undefined;
			let viewport: DOMRectReadOnly | null = null;
			let listening = false;
			function report(next: boolean) {
				if (visible === next) return;
				visible = next;
				if (next) onVisible(id); else onHidden(id);
			}
			function checkScroll() {
				if (!viewport) return;
				const area = visibleArea(element, viewport);
				const rect = element.getBoundingClientRect();
				const shown = Math.max(0, Math.min(rect.bottom, area.bottom) - Math.max(rect.top, area.top));
				report(fillsArea(shown, area.height));
			}
			function stop() {
				if (listening) document.removeEventListener('scroll', checkScroll, true);
				listening = false;
			}
			const observer = new IntersectionObserver((entries) => {
				const entry = entries.at(-1);
				if (!entry?.isIntersecting) { stop(); report(false); return; }
				viewport = entry.rootBounds;
				const area = viewport ? visibleArea(element, viewport) : null;
				if (area && entry.boundingClientRect?.height > area.height) {
					if (!listening) { document.addEventListener('scroll', checkScroll, { capture: true, passive: true }); listening = true; }
					checkScroll();
				} else {
					stop();
					report(entry.intersectionRatio === 1 || !!area && fillsArea(entry.intersectionRect.height, area.height));
				}
			}, { threshold: Array.from({ length: 21 }, (_, index) => index / 20) });
			observers.set(id, { element, observer, stop });
			observer.observe(element);
		}
	}
	return { clear, sync };
}
