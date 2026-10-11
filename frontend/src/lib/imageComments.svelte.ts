import { fromStore } from 'svelte/store';
import { createCommentSurfaceState, type CommentAnchor } from '$lib/docCommentSurfaceState.svelte';
import { createCommentRetryToast, createThreadActions, submitDocumentComment } from '$lib/docViewerOperations';
import { isPoint, type ImageRegion } from '$lib/imageRegions';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { addRegionComment, moveRegion } from '$lib/stores/docComments';
import { toasts } from '$lib/stores/toast';

const WHOLE = (region: ImageRegion) => region.x === 0 && region.y === 0 && region.w === 1 && region.h === 1;
/** What the composer says a new comment is on, in place of a quote. */
export function regionLabel(region: ImageRegion): string {
	return WHOLE(region) ? 'The whole image' : isPoint(region) ? 'Point on the image' : 'Box on the image';
}

/**
 * The image viewer's comment state (DCOL-FR13.3): box mode, the region being
 * written, moving a stale pin, and which comment is picked. Cards, the drawer
 * and the composer are the document's own, through its comment surface.
 */
export function createImageComments(context: {
	artifactId: () => string | null;
	readOnly: () => boolean;
	/** Where focus goes when a card closes with nothing else to return to. */
	restoreFocus: () => void;
	/** Scroll a region into view at the current zoom. */
	reveal?: (region: ImageRegion) => void;
	/** A comment or reply was posted, so the review status may change. */
	onCreated?: () => void;
}) {
	// Its own draft scope, so closing a card here never aborts a document's comment capture elsewhere.
	const ui = createCommentSurfaceState(context.restoreFocus, () => `image-comments:${context.artifactId() ?? ''}`);
	const focused = fromStore(focusedDocComment);
	const retryToast = createCommentRetryToast(toasts);
	const threads = createThreadActions(context.artifactId, () => context.onCreated?.());
	let boxMode = $state(false);
	let draftRegion = $state<ImageRegion | null>(null);
	let draftBody = $state('');
	let moving = $state<string | null>(null);

	function show(id: string, anchor: CommentAnchor) {
		focusedDocComment.set(id);
		ui.anchor = anchor;
		ui.state = { kind: 'thread', id };
	}
	function close() {
		ui.close();
		draftRegion = null;
		draftBody = '';
	}
	/** The Comment button: box mode on, or off again without placing anything. */
	function toggleBoxMode() {
		if (context.readOnly()) return;
		boxMode = !boxMode;
		if (!boxMode) moving = null;
	}
	/** A box, point or whole-image region was drawn: it opens the composer, or moves the pin being moved. */
	async function draw(region: ImageRegion, anchor: CommentAnchor) {
		const artifact = context.artifactId();
		if (context.readOnly() || !artifact) return;
		// One comment per press of Comment, so the next one-finger pan draws nothing.
		boxMode = false;
		const pin = moving;
		if (pin) {
			moving = null;
			try {
				await submitDocumentComment(() => moveRegion(artifact, pin, region), retryToast.remember, () => "Couldn't move the pin — try again");
				retryToast.dismissAfterSuccess();
				show(pin, anchor);
			} catch { /* The toast says why; the pin stays where it was. */ }
			return;
		}
		draftRegion = region;
		draftBody = '';
		ui.openDraft(anchor);
	}
	async function submit(text: string) {
		const artifact = context.artifactId(), region = draftRegion, anchor = ui.anchor;
		if (!artifact || !region) return;
		// The composer keeps the text when this rejects.
		const id = await submitDocumentComment(() => addRegionComment(artifact, region, text), retryToast.remember);
		retryToast.dismissAfterSuccess();
		draftRegion = null;
		draftBody = '';
		context.onCreated?.();
		if (anchor) show(id, anchor);
	}
	/** A tag was picked: its card opens beside its box, and closing it returns focus to the tag. */
	function pick(id: string, anchor: () => DOMRect | null, opener: HTMLElement) {
		draftRegion = null;
		ui.focusMarker([id], opener, anchor);
	}
	/** A card in the drawer asked to be shown on the image: the drawer stays, the tag is picked. */
	function showFromList(id: string, region: ImageRegion | null | undefined) {
		focusedDocComment.set(id);
		if (region) context.reveal?.(region);
	}
	/** Move pin: the next box or point drawn becomes the comment's new place. */
	function movePin(id: string) {
		if (context.readOnly()) return;
		close();
		moving = id;
		boxMode = true;
	}
	/**
	 * Escape closes an open card or composer first, then leaves box mode, and
	 * only then reaches the viewer's own Back. It runs in the capture phase so
	 * it decides before the viewer and the card each see the key.
	 */
	function onKeydownCapture(event: KeyboardEvent) {
		if (event.key !== 'Escape' || event.defaultPrevented) return;
		if (ui.state) {
			event.preventDefault();
			close();
			ui.restoreFocus();
		} else if (boxMode || moving) {
			event.preventDefault();
			boxMode = false;
			moving = null;
		}
	}

	return {
		ui,
		get boxMode() { return boxMode; },
		get moving() { return moving; },
		get draftRegion() { return draftRegion; },
		get selectedId() {
			if (ui.state?.kind === 'thread') return ui.state.id;
			return ui.state?.kind === 'list' ? focused.current : null;
		},
		/** A card or the composer is open over the image. */
		get cardOpen() { return ui.state?.kind === 'thread' || ui.state?.kind === 'draft'; },
		/** The composer has text, so the review bar must not answer over it. */
		get hasDraftText() { return !!draftRegion && draftBody.trim() !== ''; },
		get draft() { return draftRegion ? { text: '', label: regionLabel(draftRegion), body: draftBody } : null; },
		setDraftBody(text: string) { draftBody = text; },
		threads,
		close, toggleBoxMode, draw, submit, pick, showFromList, movePin, onKeydownCapture,
	};
}
export type ImageComments = ReturnType<typeof createImageComments>;
