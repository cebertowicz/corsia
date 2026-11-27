import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { createCommentSurfaceState, type CommentAnchor } from '$lib/docCommentSurfaceState.svelte';
import { DIAGRAM_CHANGED_MESSAGE, isDiagramChanged, postFailureMessage } from '$lib/commentFailureMessage';
import { submitDocumentComment } from '$lib/docViewerOperations';
import { createThreadActions } from '$lib/threadActions';
import { createImagePicks } from '$lib/imageCommentPicks.svelte';
import { regionLabel, type ImageRegion } from '$lib/imageRegions';
import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { addRegionComment, moveRegion, type ImagePress } from '$lib/stores/imageRegionComments';
import { forgetDraft, timedPost } from '$lib/stores/commentPostTiming';
import { EARLIER_VERSION_POSTED } from '$lib/stores/docComments';
import { imageDraftKey, keptCommentDrafts, type DraftOrigin } from '$lib/stores/keptCommentDrafts.svelte';
import { toasts } from '$lib/stores/toast';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import { unsentOutcome } from '$lib/unsentCommentOutcome';
import type { DraftSend } from '$lib/stores/keptCommentDraftsStorage';
import type { KeptPlace } from '$lib/keptPlace';

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
	/**
	 * What a press's post needs with no box open (`unsentComments`), read as a box is drawn and pressed; for an image inside
	 * a document, its markdown as a place only when it can be told apart (`uniquePlaceOf`). None: it waits for the person.
	 */
	where?: () => { send: DraftSend; place?: KeptPlace | null } | null;
}) {
	const ui = createCommentSurfaceState(context.restoreFocus);
	const key = () => context.draftKey?.() ?? imageDraftKey(context.artifactId());
	/** The toast telling why this viewer's last pin move failed: a later failure replaces it, a move that lands dismisses it. */
	let pinToast: string | null = null;
	const tellPin = (id: string | null) => { if (pinToast) toasts.dismiss(pinToast); pinToast = id; };
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
	/** The kept draft the open box shows (`imageDraftKey`): the image it was drawn on. */
	let boxKey = '';
	/** The open box's image markdown as a place, read as it was drawn (`where`); undefined for an image artifact. */
	let drawnPlace: KeptPlace | null | undefined;
	/** A stale pin is either awaiting a placement or visibly saving the newly drawn one. */
	let moving = $state<{ id: string; region?: ImageRegion } | null>(null);
	const picks = createImagePicks(ui, { clearDraft: () => { draftRegion = null; }, reveal: (...args) => context.reveal?.(...args) });

	function show(id: string, anchor: CommentAnchor) {
		focusedDocComment.set(id);
		ui.anchor = anchor;
		ui.state = { kind: 'thread', id };
	}
	/** Every way out but Cancel: the text and its tags stay kept for the next box drawn on this image. */
	function close() {
		picks.clear();
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
				// No box says why a pin did not move, so its failure is always a toast.
				await submitDocumentComment(() => moveRegion(artifact, move.id, region, context.diagramSource?.()), tellPin, () => 'Couldn’t move the pin — try again');
				tellPin(null);
				moving = null;
				show(move.id, anchor);
			} catch (error) {
				if (await refreshAfterStale(error)) { moving = { id: move.id }; boxMode = true; }
				else moving = null;
			}
			return;
		}
		draftRegion = region;
		drawnPlace = context.where?.()?.place;
		boxKey = key();
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
		// The draft this press posts, kept under its own image's key even if another image is shown before it ends.
		const post = context.post?.(), tags = mentionSelection, draftKey = key();
		keptCommentDrafts.clearError(draftKey);
		draftError = undefined;
		let id: string, failure = '', toastId: string | null = null;
		// Every press on this draft posts under its one id, so a press after an unanswered one never stores it twice.
		// From the press, a draft that knows where it goes is sent on its own if this post does not go, until it posts, is
		// refused or is edited (`unsentComments`); its box reads "Sending…" meanwhile.
		const press: ImagePress = keptCommentDrafts.claim(draftKey, text, tags, draftOrigin, context.where?.()?.send);
		const pressed = keptCommentDrafts.get(draftKey), place = drawnPlace;
		if (pressed) keptCommentDrafts.keep(draftKey, pressed.body, pressed.mentionSelection, place, region);
		let waits = false;
		// The composer that pressed is still the open one. Its region is its identity: a box drawn while this post is out is
		// a new region, while its tags can be the same object, carried over from the kept draft.
		const pressing = () => draftRegion === region;
		// The open box shows this draft: the one that pressed, or one drawn from the kept draft since. Never a box on another image.
		const showsThis = () => !!draftRegion && boxKey === draftKey;
		// A failure says why in that box when it is on screen, and nowhere else.
		const inBox = () => showsThis() && keptCommentDrafts.onScreen(draftKey);
		// What the field holds now, not the text this press sent: typing while it was out is never lost.
		const now = () => keptCommentDrafts.get(draftKey);
		try {
			id = await timedPost(press.clientId, () => submitDocumentComment(
				async () => {
					try {
						return await (post ? post.create(region, text, taggedAgentIds, mentions, press) : addRegionComment(artifact, region, text, taggedAgentIds, context.threadId?.() ?? null, mentions, press));
					} catch (error) {
						// A failure that passes, on a draft that goes on its own, is not told: it is tried again as `unsentComments` says.
						waits = unsentOutcome(error) === 'retry' && unsentComments.sending(draftKey);
						throw error;
					}
				},
				(shown, message) => { if (!waits) { toastId = shown; failure = message; } },
				(error) => (isDiagramChanged(error) ? DIAGRAM_CHANGED_MESSAGE : (post?.failure ?? postFailureMessage)(error)),
				// A changed diagram is told once its refresh ends (below): whether a box is left to say why depends on it.
				(error) => waits || isDiagramChanged(error) || inBox(),
			));
		} catch (error) {
			const changed = isDiagramChanged(error);
			if (await refreshAfterStale(error)) {
				// The pin goes on the redrawn diagram: the text and its tags carry over to the box drawn next, and the toast says why.
				const held = now();
				draftBody = held?.body ?? text;
				// Kept for the redrawn diagram (its key names the new snapshot), which the next box is drawn on; it waits for that
				// pin, so it is not sent on its own meanwhile.
				keptCommentDrafts.unsend(draftKey);
				keptCommentDrafts.keep(key(), draftBody, held?.mentionSelection ?? tags, undefined, null);
				draftRegion = null;
				replacingDraft = true;
				ui.close();
				boxMode = true;
				// The carried text holds the toast: the box drawn next on the new diagram does not say why, so it stays up.
				keptCommentDrafts.toast(draftKey, null);
				keptCommentDrafts.toast(key(), toasts.show(failure, 'error'));
			} else if (failure) {
				const held = now();
				keptCommentDrafts.fail(draftKey, held?.body ?? text, held?.mentionSelection ?? tags, failure, failure === EARLIER_VERSION_POSTED);
				// A failure that is told waits for the person: the draft does not go on its own after it.
				keptCommentDrafts.unsend(draftKey);
				// A changed diagram that could not be redrawn leaves the box open: told in it when it is on screen, else in a toast.
				// The draft holds its toast until its box comes on screen to say why, or it is posted.
				keptCommentDrafts.toast(draftKey, changed && !inBox() ? toasts.show(failure, 'error') : toastId);
				// The box showing this draft says why, never a box on another image.
				if (showsThis()) draftError = failure;
			}
			throw error;
		}
		// Text or tags changed since the press stay kept, with a fresh id for their own post. Either way a toast that told an
		// earlier press failed goes.
		const stored = keptCommentDrafts.posted(draftKey, text, taggedAgentIds);
		context.onCreated?.();
		// The box that pressed closes. A box drawn meanwhile is that draft too once what it shows is stored, and closes the
		// same way; one whose text or tags changed keeps them.
		if (!pressing() && !(stored && showsThis())) return;
		draftRegion = null;
		draftOrigin = undefined;
		draftBody = '';
		draftError = undefined;
		mentionSelection = createMentionSelection();
		if (anchor) show(id, anchor);
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
		get listPicks() { return picks.listPicks; },
		get listCandidates() { return picks.listCandidates; },
		get listCandidateOrder() { return picks.listCandidateOrder; },
		get draftRegion() { return draftRegion; },
		get selectedId() { return picks.selectedId; },
		/** A card or the composer is open over the image. */
		get cardOpen() { return ui.state?.kind === 'thread' || ui.state?.kind === 'draft'; },
		/** The composer has text, so the review bar must not answer over it. */
		get hasDraftText() { return (!!draftRegion || replacingDraft) && draftBody.trim() !== ''; },
		get draft() { return draftRegion ? { text: '', label: regionLabel(draftRegion), body: draftBody, mentionSelection, error: draftError } : null; },
		setDraftBody(text: string) { draftBody = text; keptCommentDrafts.keep(key(), text, mentionSelection, drawnPlace, draftRegion); },
		threads,
		close, cancel, toggleBoxMode, draw, submit, movePin, onKeydownCapture,
		openList: picks.openList, pick: picks.pick, pickCandidates: picks.pickCandidates, select: picks.select, showFromList: picks.showFromList,
	};
}
export type ImageComments = ReturnType<typeof createImageComments>;
