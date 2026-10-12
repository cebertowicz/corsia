import type { CommentSurfaceState } from '$lib/docCommentSurfaceState.svelte';

export type SheetDraft = { text: string; body?: string; label?: string } | null;

/**
 * The comment surface's sheet form, on AdaptiveDrawer's open/onClosed lifecycle: closing keeps what the sheet shows
 * on screen through the drawer's outro, then runs the close. The comment list takes the sheet whenever `sheetDrawer`
 * says so (the canvas-width rule); a cancelled draft plays its outro only where cards are sheets, on a `narrow` canvas.
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
	let mobileCloseAction = $state<(() => void) | null>(null);
	let closingMobileState = $state<CommentSurfaceState>(null);
	let closingMobileDraft = $state<SheetDraft>(null);
	const renderedMobileState = $derived(closingMobileState ?? props.state());
	const renderedMobileDraft = $derived(closingMobileDraft ?? props.draft());
	$effect(() => {
		if (props.state() && props.sheetDrawer()) {
			closingMobileState = null;
			closingMobileDraft = null;
			mobileDrawerOpen = true;
		}
	});
	function closeMobileDrawer(action: () => void = props.onClose): void {
		mobileCloseAction = action;
		mobileDrawerOpen = false;
	}
	function closeMobileSurface(): void {
		if (props.sheetDrawer()) closeMobileDrawer();
		else props.onClose();
	}
	function cancelMobileDraft(): void {
		const action = props.onCancelDraft;
		if (!props.narrow()) { action(); return; }
		// Cancel commits state immediately, but the captured content owns the local outro.
		closingMobileState = props.state();
		closingMobileDraft = props.draft();
		mobileCloseAction = null;
		mobileDrawerOpen = false;
		action();
	}
	function finishMobileClose(): void {
		const action = mobileCloseAction;
		mobileCloseAction = null;
		closingMobileState = null;
		closingMobileDraft = null;
		action?.();
	}
	return {
		/** The drawer's `open`: false from a close until the next opening. */
		get open() { return mobileDrawerOpen; },
		/** What the sheet shows: the closing content through its outro, otherwise the surface's own state and draft. */
		get state() { return renderedMobileState; },
		get draft() { return renderedMobileDraft; },
		/** A cancelled draft is still playing its outro. */
		get closing() { return closingMobileState !== null; },
		close: closeMobileSurface,
		cancelDraft: cancelMobileDraft,
		finishClose: finishMobileClose,
	};
}
