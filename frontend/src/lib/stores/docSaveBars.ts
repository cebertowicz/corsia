/**
 * The areas on the page that show a document's save error beside its text, by the draft's key (a tab instance). A failed
 * save says why in one place: in that bar when it is on screen at that moment, else in a toast (document closed, tab
 * hidden behind another).
 */
const bars = new Map<string, Set<Element>>();
function register(key: string, bar: Element): () => void {
	if (!key) return () => {};
	let found = bars.get(key);
	if (!found) bars.set(key, found = new Set());
	found.add(bar);
	return () => { found.delete(bar); if (!found.size && bars.get(key) === found) bars.delete(key); };
}
/** Whether `bar` takes room inside what the person sees now (the visual viewport: a phone's keyboard and pinch zoom shrink it). */
function onScreen(bar: Element): boolean {
	const rect = bar.getBoundingClientRect(), view = window.visualViewport;
	const left = view?.offsetLeft ?? 0, top = view?.offsetTop ?? 0, width = view?.width ?? window.innerWidth, height = view?.height ?? window.innerHeight;
	return rect.width > 0 && rect.height > 0 && rect.right > left && rect.left < left + width && rect.bottom > top && rect.top < top + height;
}

/** `use:showsSaveBar={key}` on the editor's wrapper: registered under the tab's key while mounted (`docSaveBars.shown`). */
export function showsSaveBar(node: Element, key: string) {
	let drop = register(key, node);
	return {
		update(next: string) { drop(); drop = register(next, node); },
		destroy() { drop(); },
	};
}

export const docSaveBars = {
	/** A bar for this key is mounted at `bar` until the returned function is called (`showsSaveBar` calls it on unmount). */
	shown(key: string, bar: Element): () => void { return register(key, bar); },
	/** A bar for this key is on the page and on screen now: one read at the moment of asking, nothing observed. */
	onScreen(key: string): boolean {
		for (const bar of (key && bars.get(key)) || []) if (onScreen(bar)) return true;
		return false;
	},
	/** Tests only: a fresh page. */
	reset(): void { bars.clear(); },
};
