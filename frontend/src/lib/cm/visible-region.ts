import type { EditorView } from '@codemirror/view';

export type Box = { top: number; bottom: number };
export type Rect = Box & { left: number; right: number };

// The scrolling pane can clip an editor that grows to the full content height.
const clipping = new Set(['auto', 'scroll', 'hidden', 'clip']);

/**
 * The part of the window that `from` and its ancestors leave showing: the window intersected with every element from `from` up
 * that clips its content, on each axis it clips. `scroller` is the nearest of them that scrolls vertically now.
 */
export function clippedWindow(from: Element): { rect: Rect; scroller: HTMLElement | null } {
	const win = from.ownerDocument.defaultView ?? window;
	const rect: Rect = { top: 0, left: 0, bottom: win.innerHeight, right: win.innerWidth };
	let scroller: HTMLElement | null = null;
	for (let element: Element | null = from; element; element = element.parentElement) {
		const { overflowX, overflowY } = getComputedStyle(element);
		if (!scroller && element instanceof HTMLElement && (overflowY === 'auto' || overflowY === 'scroll') && element.scrollHeight > element.clientHeight + 1) scroller = element;
		const x = clipping.has(overflowX), y = clipping.has(overflowY);
		if (!x && !y) continue;
		const box = element.getBoundingClientRect();
		if (y) { rect.top = Math.max(rect.top, box.top); rect.bottom = Math.min(rect.bottom, box.bottom); }
		if (x) { rect.left = Math.max(rect.left, box.left); rect.right = Math.min(rect.right, box.right); }
	}
	return { rect, scroller };
}

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
export function restingTop(panel: HTMLElement): number | null {
	const backdrop = panel.closest<HTMLElement>('[data-adaptive-drawer]');
	return panel.isConnected && backdrop ? backdrop.getBoundingClientRect().bottom - panel.offsetHeight : null;
}

/** The registered sheet over this editor while it is up, else null. */
export function overlaySheet(view: EditorView): HTMLElement | null {
	for (let element: HTMLElement | null = view.dom; element; element = element.parentElement) {
		const panel = sheets.get(element)?.();
		if (panel) return panel;
	}
	return null;
}

function overlayTop(view: EditorView): number | null {
	const panel = overlaySheet(view);
	return panel ? restingTop(panel) : null;
}

/** Whether a registered sheet overlays this editor now, so the reader's band is shorter than the pane. */
export const sheetOverlays = (view: EditorView): boolean => overlayTop(view) !== null;

/**
 * The one answer to which part of the editor the reader can see and which element scrolls it, for every vertical comment scroll.
 * `band` is the editor intersected with every clipping ancestor and the viewport, less the sheet that overlays it. `scroller` is the
 * nearest scrolling ancestor: the pane, when the editor's own scroller grows to the full content height.
 */
export function visibleRegion(view: EditorView): { scroller: HTMLElement; band: Box } {
	const { rect, scroller } = clippedWindow(view.scrollDOM);
	const top = rect.top;
	let bottom = rect.bottom;
	const sheet = overlayTop(view);
	if (sheet !== null) bottom = Math.min(bottom, sheet);
	return { scroller: scroller ?? view.scrollDOM, band: { top, bottom } };
}
