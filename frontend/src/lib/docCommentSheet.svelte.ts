import { untrack } from 'svelte';
import type { CommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';

export type SheetDraft = { text: string; body?: string; label?: string } | null;

/**
 * The comment surface's sheet form, on AdaptiveDrawer's open/onClosed lifecycle: a close commits at once, and the sheet
 * keeps what it showed on screen through the drawer's outro. The comment list takes the sheet whenever `sheetDrawer`
 * says so (the canvas-width rule); a card or the composer is a sheet only on a `narrow` canvas, so a cancelled draft
 * plays its outro only there. However the surface closes, from the sheet or by its own state turning null (Escape, the
 * header's close, a deleted thread), the sheet plays the same outro before it unmounts.
 */
export function createCommentSheet(props: {
	state: () => CommentSurfaceState;
	draft: () => SheetDraft;
	sheetDrawer: () => boolean;
	narrow: () => boolean;
	onClose: () => void;
	onCancelDraft: () => void;
}) {
	let mobileDrawerOpen = $state(false);
	let closingMobileState = $state<CommentSurfaceState>(null);
	let closingMobileDraft = $state<SheetDraft>(null);
	const renderedMobileState = $derived(closingMobileState ?? props.state());
	const renderedMobileDraft = $derived(closingMobileDraft ?? props.draft());
	/** What the sheet last showed, kept for an outro when the surface's state turns null under it. */
	let shownState: CommentSurfaceState = null;
	let shownDraft: SheetDraft = null;
	// The list is a sheet by the canvas-width rule; a card or the composer only on a narrow canvas.
	const drawnAsSheet = (state: CommentSurfaceState) => !!state && (state.kind === 'list' ? props.sheetDrawer() : props.narrow());
	$effect(() => {
		if (props.state() && props.sheetDrawer()) {
			closingMobileState = null;
			closingMobileDraft = null;
			mobileDrawerOpen = true;
		}
	});
	// The surface closed while the sheet was open (from the sheet or from outside it): show what it showed through the
	// outro, which ends in finishClose.
	// Before the DOM updates, so the drawer is never unmounted for the one frame before its outro starts.
	$effect.pre(() => {
		const state = props.state();
		const draft = props.draft();
		if (state) { shownState = state; shownDraft = draft; return; }
		untrack(() => {
			if (!mobileDrawerOpen) return;
			if (drawnAsSheet(shownState)) {
				closingMobileState = shownState;
				closingMobileDraft = shownDraft;
			}
			mobileDrawerOpen = false;
		});
	});
	// The sheet's own close (its scrim, a drag, Escape or Back inside it) commits the surface's close at once, like
	// Escape or the header's close do; the effect above carries the outro, so every close is one path and the canvas
	// moves with the sheet as it leaves, not after it.
	function closeMobileSurface(): void {
		props.onClose();
	}
	function cancelMobileDraft(): void {
		const action = props.onCancelDraft;
		if (!props.narrow()) { action(); return; }
		// Cancel commits state immediately, but the captured content owns the local outro.
		closingMobileState = props.state();
		closingMobileDraft = props.draft();
		mobileDrawerOpen = false;
		action();
	}
	function finishMobileClose(): void {
		closingMobileState = null;
		closingMobileDraft = null;
	}
	return {
		/** The drawer's `open`: false from a close until the next opening. */
		get open() { return mobileDrawerOpen; },
		/** The modal is visible until its outro finishes, including the scrim. */
		get modalOpen() { return (mobileDrawerOpen && drawnAsSheet(props.state())) || closingMobileState !== null; },
		/** What the sheet shows: the closing content through its outro, otherwise the surface's own state and draft. */
		get state() { return renderedMobileState; },
		get draft() { return renderedMobileDraft; },
		/** A sheet whose surface has already closed (a cancelled draft, or a close from outside the sheet) is still playing its outro. */
		get closing() { return closingMobileState !== null; },
		close: closeMobileSurface,
		cancelDraft: cancelMobileDraft,
		finishClose: finishMobileClose,
	};
}
