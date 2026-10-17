import type { EditorView } from '@codemirror/view';

export type Box = { top: number; bottom: number };

// The scrolling pane can clip an editor that grows to the full content height.
const clipping = new Set(['auto', 'scroll', 'hidden', 'clip']);

// A sheet overlays the editors of the surface that registered it, not whichever drawer the page holds.
const sheets = new WeakMap<Element, () => HTMLElement | null>();

/**
 * Registers the sheet (an AdaptiveDrawer panel) that overlays the editors inside `surface`; `sheet` returns it while it is up, else null.
 * A side-by-side drawer, another pane's sheet and an image's sheet are not registered, so none of them narrows an editor's region.
 * A surface that is not drawn yet registers nothing.
 */
export function trackOverlaySheet(surface: Element | null, sheet: () => HTMLElement | null): () => void {
	if (!surface) return () => {};
	sheets.set(surface, sheet);
	return () => { if (sheets.get(surface) === sheet) sheets.delete(surface); };
}

/** Where the sheet rests: its backdrop's bottom less its own height, which neither the intro's slide nor a drag moves. */
function restingTop(panel: HTMLElement): number | null {
	const backdrop = panel.closest<HTMLElement>('[data-adaptive-drawer]');
	return panel.isConnected && backdrop ? backdrop.getBoundingClientRect().bottom - panel.offsetHeight : null;
}

function overlayTop(view: EditorView): number | null {
	for (let element: HTMLElement | null = view.dom; element; element = element.parentElement) {
		const panel = sheets.get(element)?.();
		if (panel) return restingTop(panel);
	}
	return null;
}

/**
 * The one answer to which part of the editor the reader can see and which element scrolls it, for every vertical comment scroll.
 * `band` is the editor intersected with every clipping ancestor and the viewport, less the sheet that overlays it. `scroller` is the
 * nearest scrolling ancestor: the pane, when the editor's own scroller grows to the full content height.
 */
export function visibleRegion(view: EditorView): { scroller: HTMLElement; band: Box } {
	let top = 0;
	let bottom = view.dom.ownerDocument.defaultView?.innerHeight ?? window.innerHeight;
	let scroller: HTMLElement | null = null;
	for (let element: HTMLElement | null = view.scrollDOM; element; element = element.parentElement) {
		const { overflowY } = getComputedStyle(element);
		if (!scroller && (overflowY === 'auto' || overflowY === 'scroll') && element.scrollHeight > element.clientHeight + 1) scroller = element;
		if (!clipping.has(overflowY)) continue;
		const rect = element.getBoundingClientRect();
		top = Math.max(top, rect.top);
		bottom = Math.min(bottom, rect.bottom);
	}
	const sheet = overlayTop(view);
	if (sheet !== null) bottom = Math.min(bottom, sheet);
	return { scroller: scroller ?? view.scrollDOM, band: { top, bottom } };
}
