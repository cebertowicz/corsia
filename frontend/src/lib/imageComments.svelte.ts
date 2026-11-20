import { tick } from 'svelte';
import { fromStore } from 'svelte/store';
import { RequestRefused } from '$lib/api/docs';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { createCommentSurfaceState, type CommentAnchor } from '$lib/docCommentSurfaceState.svelte';
import { createCommentRetryToast, createThreadActions, submitDocumentComment } from '$lib/docViewerOperations';
import { isPoint, type ImageRegion } from '$lib/imageRegions';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { pickedComments } from '$lib/stores/docComments';
import { addRegionComment, moveRegion, type ImagePress } from '$lib/stores/imageRegionComments';
import { forgetDraft, timedPost } from '$lib/stores/commentPostTiming';
import { EARLIER_VERSION_POSTED } from '$lib/stores/docComments';
import { imageDraftKey, keptCommentDrafts, type DraftOrigin } from '$lib/stores/keptCommentDrafts.svelte';
import { toasts } from '$lib/stores/toast';

/** What a person is told when the diagram changed under their pin; the server's `diagram_changed` refusal carries the same words. */
export const DIAGRAM_CHANGED_MESSAGE = 'Place the pin on the current diagram; your draft is kept.';
const isDiagramChanged = (error: unknown) => error instanceof RequestRefused && error.kind === 'diagram_changed';

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
	/** The image a new comment's text is kept for (`imageDraftKey`); the image artifact by default. */
	draftKey?: () => string;
	threadId?: () => string | null;
	readOnly: () => boolean;
	diagramSource?: () => string | undefined;
	/** Refresh the rendered diagram before asking for a replacement placement. */
	refreshDiagram?: () => Promise<boolean>;
	/** Where focus goes when a card closes with nothing else to return to. */
	restoreFocus: () => void;
	/** Scroll a comment's region and its tag into view at the current zoom. */
	reveal?: (id: string, region: ImageRegion, fromList?: boolean) => void;
	/** A comment or reply was posted, so the review status may change. */
	onCreated?: () => void;
	/**
	 * Posts a new region comment; an image inside a document anchors it on the image's markdown. Without it the
	 * comment goes on the image artifact. `failure` words what went wrong for the toast.
	 */
	post?: () => { create: (region: ImageRegion, text: string, taggedAgentIds: string[], mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[], press: ImagePress) => Promise<string>; failure?: (error: unknown) => string } | null;
}) {
	const ui = createCommentSurfaceState(context.restoreFocus);
	const key = () => context.draftKey?.() ?? imageDraftKey(context.artifactId());
	const focused = fromStore(focusedDocComment);
	const retryToast = createCommentRetryToast(toasts);
	const threads = createThreadActions(context.artifactId, () => context.onCreated?.(), () => context.threadId?.() ?? null);
	let boxMode = $state(false);
	let draftRegion = $state<ImageRegion | null>(null);
	let draftBody = $state('');
	/** Why the last press of Comment did not post; the composer shows it until the next press. */
	let draftError = $state<string | undefined>(undefined);
	let replacingDraft = $state(false);
	let mentionSelection = createMentionSelection();
	/** The kept draft the open box was drawn from, if Comment was pressed on it (`keptCommentDrafts.origin`). */
	let draftOrigin: DraftOrigin | undefined;
	/** A stale pin is either awaiting a placement or visibly saving the newly drawn one. */
	let moving = $state<{ id: string; region?: ImageRegion } | null>(null);
	/** Counts tags picked with the drawer open, so picking the selected one again still brings its card back. */
	let listPicks = $state(0);
	let listCandidates = $state<string[] | null>(null);
	let listCandidateOrder = $state<string[] | null>(null);

	function show(id: string, anchor: CommentAnchor) {
		focusedDocComment.set(id);
		ui.anchor = anchor;
		ui.state = { kind: 'thread', id };
	}
	/** Every way out but Cancel: the text and its tags stay kept for the next box drawn on this image. */
	function close() {
		listCandidates = null;
		listCandidateOrder = null;
		ui.close();
		draftRegion = null;
		replacingDraft = false;
	}
	/** The composer's Cancel, the one way to drop typed text. */
	function cancel() {
		forgetDraft(keptCommentDrafts.get(key())?.clientId);
		keptCommentDrafts.forget(key());
		close();
		draftBody = '';
		draftError = undefined;
		mentionSelection = createMentionSelection();
	}
	/** The Comment button: box mode on, or off again without placing anything. */
	function toggleBoxMode() {
		if (context.readOnly() || moving?.region) return;
		boxMode = !boxMode;
		if (!boxMode) moving = null;
	}
	async function refreshAfterStale(error: unknown): Promise<boolean> {
		return isDiagramChanged(error) ? await context.refreshDiagram?.() ?? false : false;
	}
	/** A box, point or whole-image region was drawn: it opens the composer, or moves the pin being moved. */
	async function draw(region: ImageRegion, anchor: CommentAnchor) {
		const artifact = context.artifactId();
		if (context.readOnly() || !artifact || moving?.region) return;
		// One comment per press of Comment, so the next one-finger pan draws nothing.
		boxMode = false;
		const move = moving;
		if (move) {
			// Keep the new location visible until the server accepts it or rejects it.
			moving = { ...move, region };
			try {
				await submitDocumentComment(() => moveRegion(artifact, move.id, region, context.diagramSource?.()), retryToast.remember, () => "Couldn't move the pin — try again");
				retryToast.dismissAfterSuccess();
				moving = null;
				show(move.id, anchor);
			} catch (error) {
				if (await refreshAfterStale(error)) { moving = { id: move.id }; boxMode = true; }
				else moving = null;
			}
			return;
		}
		draftRegion = region;
		// The composer starts from what the last one on this image held: its text, its tags and why it did not post.
		const kept = keptCommentDrafts.get(key());
		draftBody = kept?.body ?? '';
		draftError = kept?.error;
		// Its own copy of the tags: the box before it clears its own when its post lands.
		mentionSelection = kept?.mentionSelection.copy() ?? createMentionSelection(draftBody);
		draftOrigin = keptCommentDrafts.origin(key());
		replacingDraft = false;
		ui.openDraft(anchor);
	}
	async function submit(text: string, taggedAgentIds: string[] = [], mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[] = []) {
		const artifact = context.artifactId(), region = draftRegion, anchor = ui.anchor;
		if (!artifact || !region) return;
		// The composer keeps the text when this rejects.
		const post = context.post?.(), tags = mentionSelection;
		keptCommentDrafts.clearError(key());
		draftError = undefined;
		let id: string, failure = '';
		// Every press on this draft posts under its one id, so a press after an unanswered one never stores it twice.
		const press: ImagePress = keptCommentDrafts.claim(key(), text, tags, draftOrigin);
		// The composer that pressed is still the open one. Its region is its identity: a box drawn while this post is out is
		// a new region, while its tags can be the same object, carried over from the kept draft.
		const pressing = () => draftRegion === region;
		// What the field holds now, not the text this press sent: typing while it was out is never lost.
		const now = () => keptCommentDrafts.get(key());
		try {
			id = await timedPost(press.clientId, () => submitDocumentComment(
				() => (post ? post.create(region, text, taggedAgentIds, mentions, press) : addRegionComment(artifact, region, text, taggedAgentIds, context.threadId?.() ?? null, mentions, press)),
				(toastId, message) => { retryToast.remember(toastId); failure = message; },
				(error) => (isDiagramChanged(error) ? DIAGRAM_CHANGED_MESSAGE : post?.failure?.(error)),
			));
		} catch (error) {
			if (await refreshAfterStale(error)) {
				// The pin goes on the redrawn diagram: the text and its tags carry over to the box drawn next.
				const held = now();
				draftBody = held?.body ?? text;
				keptCommentDrafts.keep(key(), draftBody, held?.mentionSelection ?? tags);
				draftRegion = null;
				replacingDraft = true;
				ui.close();
				boxMode = true;
			} else if (failure) {
				const held = now();
				keptCommentDrafts.fail(key(), held?.body ?? text, held?.mentionSelection ?? tags, failure, failure === EARLIER_VERSION_POSTED);
				if (pressing()) draftError = failure;
			}
			throw error;
		}
		retryToast.dismissAfterSuccess();
		// Text or tags changed since the press stay kept, with a fresh id for their own post.
		const stored = keptCommentDrafts.posted(key(), text, taggedAgentIds);
		context.onCreated?.();
		// The box that pressed closes. A box drawn meanwhile is that draft too once what it shows is stored, and closes the
		// same way; one whose text or tags changed keeps them.
		if (!pressing() && !(stored && draftRegion)) return;
		draftRegion = null;
		draftOrigin = undefined;
		draftBody = '';
		draftError = undefined;
		mentionSelection = createMentionSelection();
		if (anchor) show(id, anchor);
	}
	/**
	 * A tag was picked: its card opens beside its box, and closing it returns
	 * focus to the tag. With the drawer open, the drawer stays and the tag is
	 * selected there instead, so the drawer can bring its card into view. Either way it is the person's pick, which reads it.
	 */
	function pick(id: string, anchor: () => DOMRect | null, opener: HTMLElement) {
		listCandidates = null;
		draftRegion = null;
		if (ui.state?.kind === 'list') { focusedDocComment.set(id); pickedComments.set(new Set([id])); listPicks += 1; return; }
		ui.focusMarker([id], opener, anchor);
	}
	function openList(opener: HTMLElement) {
		listCandidates = null;
		listCandidateOrder = null;
		ui.openList(opener);
	}
	/** Ambiguous touch or pen: use the existing list and mark all candidates in its number order. */
	function pickCandidates(ids: string[], opener: HTMLElement) {
		draftRegion = null;
		if (ui.state?.kind !== 'list') ui.openList(opener);
		listCandidates = ids;
		listCandidateOrder = ids;
		focusedDocComment.set(ids[0]);
		listPicks += 1;
	}
	/** Open a comment's card beside its box, as a pick would: the viewer was opened on it from the document. */
	function select(id: string, tag: HTMLElement, region: ImageRegion | null | undefined, anchor?: () => DOMRect | null) {
		draftRegion = null;
		ui.focusMarker([id], tag, anchor ?? (() => (tag.isConnected ? tag.getBoundingClientRect() : null)));
		if (region) void tick().then(() => {
			if (ui.state?.kind === 'thread' && ui.state.id === id) context.reveal?.(id, region);
		});
	}
	/** A card in the drawer was selected: the drawer stays, the tag is picked and scrolled into view. */
	function showFromList(id: string, region: ImageRegion | null | undefined) {
		listCandidates = null;
		focusedDocComment.set(id);
		if (region) context.reveal?.(id, region, true);
	}
	/** Move pin: the next box or point drawn becomes the comment's new place. */
	function movePin(id: string) {
		if (context.readOnly()) return;
		close();
		moving = { id };
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
		} else if ((boxMode || moving) && !moving?.region) {
			event.preventDefault();
			boxMode = false;
			moving = null;
		}
	}

	return {
		ui,
		get boxMode() { return boxMode; },
		get moving() { return moving?.id ?? null; },
		get movingRegion() { return moving?.region ?? null; },
		get moveSaving() { return !!moving?.region; },
		get listPicks() { return listPicks; },
		get listCandidates() { return ui.state?.kind === 'list' ? listCandidates : null; },
		get listCandidateOrder() { return ui.state?.kind === 'list' ? listCandidateOrder : null; },
		get draftRegion() { return draftRegion; },
		get selectedId() {
			if (ui.state?.kind === 'thread') return ui.state.id;
			return ui.state?.kind === 'list' ? focused.current : null;
		},
		/** A card or the composer is open over the image. */
		get cardOpen() { return ui.state?.kind === 'thread' || ui.state?.kind === 'draft'; },
		/** The composer has text, so the review bar must not answer over it. */
		get hasDraftText() { return (!!draftRegion || replacingDraft) && draftBody.trim() !== ''; },
		get draft() { return draftRegion ? { text: '', label: regionLabel(draftRegion), body: draftBody, mentionSelection, error: draftError } : null; },
		setDraftBody(text: string) { draftBody = text; keptCommentDrafts.keep(key(), text, mentionSelection); },
		threads,
		close, cancel, openList, toggleBoxMode, draw, submit, pick, pickCandidates, select, showFromList, movePin, onKeydownCapture,
	};
}
export type ImageComments = ReturnType<typeof createImageComments>;
