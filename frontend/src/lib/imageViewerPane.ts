import { get } from 'svelte/store';
import { paneCollapsed, setPaneCollapsed, snapShell } from '$lib/stores/paneCollapsed';
import { isPaneDrawer, paneOpen } from '$lib/stores/paneOpen';
import { sidebarCollapsed } from '$lib/stores/ui';
import { ARTIFACT_DEFAULT_FRACTION, ARTIFACT_MIN, CHAT_MIN, DIVIDER_WIDTH, clamp } from '$lib/artifactSplitLayout';

/** The leaf width the workspace pane's collapse gives (artifactSplitLayout's default cap); at or above it there is nothing to gain. */
export const WIDE_ENOUGH_LEAF = 600;

/**
 * Gives an open image viewer the width of a collapsed workspace pane, and returns the way to hand it back.
 *
 * It changes only what the user did not already have: a pane that was collapsed stays so, and only what it
 * changed is restored. It does nothing at drawer widths (the pane is a drawer there), nor when the user has
 * already made the leaf wide. If the user toggles the pane while the viewer is open their choice wins: nothing
 * is restored. The returned function is safe to call more than once.
 *
 * The pane eases to its new width unless `instant` is set: a tab switch puts the canvas at its tab's width at once,
 * so the column beside it never moves while the person is switching.
 */
export function widenForImageViewer(leafWidth: number, { instant = false } = {}): (release?: { instant?: boolean }) => void {
	if (isPaneDrawer() || leafWidth >= WIDE_ENOUGH_LEAF) return () => {};
	return hidePanes({ sidebar: false, instant });
}

/**
 * The canvas width (px) at which the History surface puts the version list beside the text: where the text keeps at least 45
 * characters a line, as for the margin comments column (45 x 6.98 px = 314 px of the body font). Beside the text sit the list's grid
 * column (320, `DocViewer.css`), the grid gap (8) and the editor's own padding (24): 314 + 320 + 8 + 24 = 666. Measured on the running
 * app: the list column is 320 and the editor text is the canvas less 352 (1920 wide, both panes hidden: canvas 1112, text 760).
 * `DocViewer.css` and `DocHistorySurface.svelte` use the same number.
 */
export const HISTORY_BESIDE_CANVAS = Math.round(45 * 6.98) + 320 + 8 + 24;

/**
 * Opening History at desktop width hides the sidebar and the Workspace panel when that
 * brings the canvas to `HISTORY_BESIDE_CANVAS` or more, and the returned function brings them back as they were (same rules as
 * `widenForImageViewer`: what the user already had hidden stays hidden, a toggle made meanwhile wins). Where hiding both still
 * leaves the canvas under it, the strip or sheet stays and so does the sidebar; only a Workspace panel the shell shows as an overlay
 * (`PaneShell`, `shouldOverlayPane`) is closed, at any width, because it takes no layout and would cover History's rows. The shell takes its new widths at once, so the
 * first frame of History is already the wide layout.
 *
 * `canvasWidth` is the document canvas now, the `doc-canvas` container's content width, and `inset` what the leaf's body adds
 * around it (its horizontal padding). The width with both hidden is the split's own rule (`canvasWithPanesHidden`), which the
 * running app measured equal at every width from 1200 to 1920 px; a divider the user dragged is not predicted.
 */
export function widenForHistory(canvasWidth: number, inset: number, { instant = true } = {}): (release?: { instant?: boolean }) => void {
	if (isPaneDrawer()) return () => {};
	const overlaid = paneIsOverlay();
	if (canvasWidth < HISTORY_BESIDE_CANVAS && canvasWithPanesHidden(window.innerWidth, inset) >= HISTORY_BESIDE_CANVAS) return hidePanes({ sidebar: true, instant });
	return overlaid ? hidePanes({ sidebar: false, instant }) : () => {};
}

/** The Workspace panel is on screen as an overlay on the canvas (`aside.pane.overlay`, set by `PaneShell`). */
function paneIsOverlay(): boolean {
	return typeof document !== 'undefined' && document.querySelector('aside.pane.overlay') !== null;
}

/** The document canvas's content width at a viewport width with the sidebar and the Workspace panel both hidden. */
export function canvasWithPanesHidden(viewportWidth: number, inset: number): number {
	return clamp(viewportWidth * ARTIFACT_DEFAULT_FRACTION, ARTIFACT_MIN, viewportWidth - CHAT_MIN - DIVIDER_WIDTH) - inset;
}

/** An element's content-box width: what its `container-type: inline-size` queries (so the 666 px line) measure. */
export function contentWidth(element: HTMLElement): { width: number; inset: number } {
	const style = getComputedStyle(element);
	const inset = (parseFloat(style.paddingLeft) || 0) + (parseFloat(style.paddingRight) || 0);
	return { width: element.clientWidth - inset, inset };
}

function hidePanes({ sidebar, instant }: { sidebar: boolean; instant: boolean }): (release?: { instant?: boolean }) => void {
	const before = { collapsed: get(paneCollapsed), open: get(paneOpen), sidebar: get(sidebarCollapsed) };
	const changed = { collapsed: !before.collapsed, open: before.open, sidebar: sidebar && !before.sidebar };
	if (!changed.collapsed && !changed.open && !changed.sidebar) return () => {};

	let ours = true;
	let userChoice = false;
	const watch = (expected: () => boolean) => (value: boolean) => {
		if (ours || value === expected()) return;
		userChoice = true;
	};
	const unsubscribers = [
		paneCollapsed.subscribe(watch(() => true)),
		paneOpen.subscribe(watch(() => false)),
		...(changed.sidebar ? [sidebarCollapsed.subscribe(watch(() => true))] : []),
	];
	if (instant) snapShell();
	if (changed.collapsed) setPaneCollapsed(true, instant);
	if (changed.open) paneOpen.set(false);
	if (changed.sidebar) sidebarCollapsed.set(true);
	ours = false;

	let released = false;
	return ({ instant = false } = {}) => {
		if (released) return;
		released = true;
		for (const stop of unsubscribers) stop();
		if (userChoice) return;
		if (changed.collapsed) setPaneCollapsed(false, instant);
		if (changed.open) paneOpen.set(true);
		if (changed.sidebar) sidebarCollapsed.set(false);
	};
}
