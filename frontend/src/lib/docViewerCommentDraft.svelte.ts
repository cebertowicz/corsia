import { untrack } from 'svelte';
import { createMentionSelection, type MentionOccurrence } from '$lib/components/comments/mentionSelection';
import { commentDraft, commentDraftScope, cancelCommentCapture, discardCommentDraft, dropCommentDraft, setCommentBody, type CommentDraft } from '$lib/stores/commentDraft';
import { documentDraftKey, keptCommentDrafts } from '$lib/stores/keptCommentDrafts.svelte';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { selectComment, selectionRange, SELECTION_LOST, SELECTION_RECONNECTED, type CommentSelection } from '$lib/stores/commentSelection';
import { postLive } from '$lib/stores/liveCommentPost';
import { forgetDraft, timed, timedPost } from '$lib/stores/commentPostTiming';
import { EARLIER_VERSION_POSTED } from '$lib/stores/docComments';
import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
import { addComment } from '$lib/stores/docComments';
import { findPlace, uniquePlaceOf } from '$lib/keptPlace';
import { unsentComments } from '$lib/stores/unsentComments.svelte';
import { unsentOutcome } from '$lib/unsentCommentOutcome';
import { DRAFT_KEPT } from '$lib/stores/draftKept';
import type { SelectionOffer } from '$lib/cm/widget-selection-types';
import { isNotCommentable, submitDocumentComment } from '$lib/docViewerOperations';
import { liveFailureMessage, postFailureMessage } from '$lib/commentFailureMessage';
import type { CommentAnchor } from '$lib/docCommentSurfaceState.svelte';

export type ViewerSelection = SelectionOffer | null;
/** What a kept comment whose passage is gone says in its box, which is on no text until the person selects where it goes. */
export const UNPLACED = `The text this comment was on changed before it sent, so it isn’t placed. ${DRAFT_KEPT} Select where it goes to put it back.`;
type Context = {
	scope: () => string;
	/** The document shown; its draft is kept under `documentDraftKey`, whichever viewer shows it. */
	target: () => DocViewerTarget | null;
	draft: () => CommentDraft | null;
	session: () => LiveDocumentSession | undefined;
	generation: () => number;
	text: () => string;
	isOpen: () => boolean;
	ensureArtifactId: () => Promise<string | null>;
	/** The document is shown with its live text, so a kept draft can come back on its passage. */
	ready: () => boolean;
	/** The offer of a kept draft's passage, selected and scrolled to; with `to` = `from`, the view's foot, for one with no place. */
	offerFor: (from: number, to: number) => SelectionOffer | null;
	onStart: (anchor: CommentAnchor) => void;
	onClose: () => void;
	/** True while a posted comment may still get its first drawn mark; false once it is drawn, cannot be placed (not_placed, orphaned) or is gone. */
	awaitsMark: (id: string) => boolean;
};
const aborted = () => new DOMException('Comment draft closed', 'AbortError');
export { liveFailureMessage, postFailureMessage };
// Once Comment is pressed the post goes on whether or not its composer is still open: closing it, another card, another
// tab or the viewer going away never aborts it. Only Cancel does (`discardCommentDraft`), and a reconnect, whose old
// session can no longer place the comment (the composer says so).
async function postLiveComment(draft: CommentDraft, text: string, context: Context, press: { clientId: string; again: boolean }, taggedAgentIds: string[], threadId: string | null, mentions: MentionOccurrence[]): Promise<string> {
	const selection = draft.selection!;
	cancelCommentCapture(draft.scope);
	const controller = new AbortController(), sequence = (draft.sequence ?? 0) + 1;
	commentDraft.update(value => value && value.identity === draft.identity ? { ...value, request: controller, sequence } : value);
	const session = selection.session;
	const version = { body: text, tagged_agent_ids: taggedAgentIds };
	const attempt = (again: boolean) => postLive({ selection, clientId: press.clientId, again, seq: sequence, signal: controller.signal, version }, (captured) => {
		if (controller.signal.aborted) throw aborted();
		return addComment(session.artifactId, captured.from, captured.to, captured.quote, text, 'you', true, captured.clientId, taggedAgentIds, threadId, mentions, press.again);
	});
	try {
		return await attempt(press.again);
	} catch (error) {
		// The server lost the range between capture and save: capture the still-tracked selection again before giving up.
		// A document that no longer takes comments is not a lost range: a second attempt would only be refused again.
		const refused = error instanceof Error && error.message.startsWith('409 ') && !isNotCommentable(error);
		if (!refused || !selectionRange(selection) || controller.signal.aborted) throw error;
		return attempt(false);
	}
}

/** Selection/composer controller. The existing scoped draft remains the sole body/request owner. */
export function createViewerCommentDraft(context: Context) {
	let pending = $state<ViewerSelection>(null);
	const keepKey = () => documentDraftKey(context.target());
	let selected: CommentSelection | undefined;
	/** Every composer this viewer opened: once a post's text is stored, one of these showing it is that draft. */
	const opened = new WeakSet<object>();
	// The pending mark of a posted widget comment, held (the draft is gone by then) until its placed mark is drawn.
	let posted = $state.raw<{ id: string; selection: CommentSelection } | null>(null);
	// The hold clears on every way out: the comment is drawn, or ends not_placed, orphaned or absent (the effect below);
	// cancel(); another document is shown (this effect's cleanup).
	$effect(() => {
		const scope = context.scope();
		return () => { posted = null; dropCommentDraft(scope); };
	});
	$effect(() => {
		const session = context.session(), scope = context.scope();
		untrack(() => {
			const draft = context.draft();
			if (draft?.scope === scope && draft.selection && draft.selection.session !== session) {
				posted = null;
				cancelCommentCapture(scope);
			}
		});
	});
	const reason = $derived.by(() => {
		context.text(); context.generation();
		const draft = context.draft(), selection = draft?.scope === context.scope() ? draft.selection : undefined;
		if (!selection) return '';
		if (selection.session !== context.session()) return SELECTION_RECONNECTED;
		return selectionRange(selection) ? '' : SELECTION_LOST;
	});
	function select(value: ViewerSelection): void {
		pending = value;
		const session = context.session();
		selected = value && session ? selectComment(session, value.from, value.to) : undefined;
	}
	// This document's draft, kept across a close (its body survives); the pending highlight shows only while its composer is open.
	const scoped = $derived.by(() => { const draft = context.draft(); return draft?.scope === context.scope() ? draft : null; });
	// A comment the user has typed but not posted: the review bar never answers over it.
	const hasText = $derived(Boolean(scoped?.body?.trim()));
	// A live tap in a widget shows its pending mark before Comment is pressed; selecting elsewhere clears it, and prose has none.
	// While the composer is open the scoped draft owns the mark: a new selection does not take it.
	// A held mark is never resolved against another session's Y doc.
	const held = $derived(posted && posted.selection.session === context.session() ? posted.selection : null);
	const pendingComment = $derived.by(() => {
		const selection = context.isOpen() ? scoped?.selection : pending?.inWidget ? selected : held;
		return selection?.session === context.session() ? selection : null;
	});
	$effect(() => { if (posted && !context.awaitsMark(posted.id)) posted = null; });
	/** The viewer loads a file: the selection pending in the last one goes. */
	function leave(previous: DocViewerTarget | null, key: string): void {
		select(null);
		// Another file lets go of the last one's draft (its text stays kept); a reload of this one keeps its own.
		if (previous && commentDraftScope(previous) !== key) dropCommentDraft(commentDraftScope(previous));
	}
	/** Cancel, the one way to drop typed text. */
	function cancel(): void {
		posted = null;
		forgetDraft(keptCommentDrafts.get(keepKey())?.clientId);
		keptCommentDrafts.forget(keepKey()); discardCommentDraft(context.scope()); context.onClose();
	}
	/** What the composer holds, kept for this document until it posts or is cancelled. */
	function setBody(body: string): void {
		setCommentBody(body, context.scope());
		const draft = scoped;
		// Its passage goes with it, so it comes back there; a box with no place leaves the kept one as it is.
		if (draft?.mentionSelection) keptCommentDrafts.keep(keepKey(), body, draft.mentionSelection, draft.unplaced ? undefined : draft.place);
	}
	function start(): void { open(false); }
	function open(unplaced: boolean): void {
		if (!pending) return;
		cancelCommentCapture(context.scope());
		// The next composer on this document starts from what the last one held, its tags and why it did not post.
		const old = context.draft(), same = old?.scope === context.scope() ? old : null, kept = keptCommentDrafts.get(keepKey());
		const body = same?.body ?? kept?.body ?? '';
		// Its own copy of the tags: the composer before it clears its own when its post lands.
		const mentionSelection = (same?.mentionSelection ?? kept?.mentionSelection)?.copy() ?? createMentionSelection(body);
		const identity = {};
		opened.add(identity);
		commentDraft.set({ from: pending.from, to: pending.to, text: pending.text, top: pending.top,
			body, mentionSelection, error: unplaced ? UNPLACED : same && !same.unplaced ? same.error : kept?.error, label: typeof pending.label === 'function' ? pending.label() : pending.label, holdUntilDrawn: pending.inWidget, scope: context.scope(), key: keepKey(), selection: selected, identity, sequence: 0, origin: keptCommentDrafts.origin(keepKey()),
			...(unplaced ? { unplaced } : { place: uniquePlaceOf(context.text(), pending.from, pending.to, pending.text) }) });
		const value = pending;
		pending = null; selected = undefined;
		context.onStart(value.anchor ?? new DOMRect(value.left, value.top, 0, 0));
	}
	async function submit(text: string, taggedAgentIds: string[] = [], threadId: string | null = null, mentions: MentionOccurrence[] = []): Promise<string | null> {
		const draft = context.draft();
		if (!draft || draft.scope !== context.scope()) return null;
		const key = keepKey(), tags = draft.mentionSelection ?? createMentionSelection(text);
		const mine = (update: (value: CommentDraft) => CommentDraft | null) => commentDraft.update(value => value && value.identity === draft.identity ? update(value) : value);
		keptCommentDrafts.clearError(key);
		mine(value => ({ ...value, error: undefined }));
		// Keep the passage as it is at the press, when it can be told apart, for an automatic resend.
		const range = draft.selection ? selectionRange(draft.selection) : { from: draft.from, to: draft.to, quote: draft.text };
		const place = draft.unplaced ? undefined : range && uniquePlaceOf(draft.selection?.session.text.toString() ?? context.text(), range.from, range.to, range.quote);
		// Every press on this draft posts under its one id, so a press after an unanswered one never stores it twice. From the
		// press, a draft with a place goes on its own if this post does not (`unsentComments`), until it posts, is refused or is edited.
		const live = draft.selection && { artifactId: draft.selection.session.artifactId, threadId };
		const press = keptCommentDrafts.claim(key, text, tags, draft.origin, live || undefined);
		const pressed = keptCommentDrafts.get(key);
		if (pressed && place !== undefined) keptCommentDrafts.keep(key, pressed.body, pressed.mentionSelection, place);
		let waits = false;
		const id = await timedPost(press.clientId, () => submitDocumentComment(async () => {
			try {
				if (draft.selection) return await postLiveComment(draft, text, context, press, taggedAgentIds, threadId, mentions);
				const artifactId = await timed(press.clientId, 'ens', () => context.ensureArtifactId());
				if (!artifactId) throw new Error('Document artifact is unavailable.');
				if (pressed) keptCommentDrafts.sends(key, press.clientId, pressed.body, { artifactId, threadId });
				return await addComment(artifactId, draft.from, draft.to, draft.text, text, 'you', false, press.clientId, taggedAgentIds, threadId, mentions, press.again);
			} catch (error) {
				// A failure that passes, on a draft that goes on its own, is not told: its box reads "Sending…", and it is tried
				// again when the document opens, the connection comes back or the person returns. Any other failure is told as before.
				waits = !(error instanceof DOMException && error.name === 'AbortError') && unsentOutcome(error) === 'retry' && unsentComments.sending(key);
				throw error;
			}
		}, (toastId, message) => {
			if (waits) return;
			// Open or closed, the composer keeps the text with why it did not post, here and in the next composer on this document.
			// What the field holds now is kept, not the text this press sent: typing while it was out is never lost.
			// Text changed since an earlier version was stored goes out as a new comment on the next press.
			const now = keptCommentDrafts.get(key);
			keptCommentDrafts.fail(key, now?.body ?? text, now?.mentionSelection ?? tags, message, message === EARLIER_VERSION_POSTED);
			// A failure that is told waits for the person: the draft does not go on its own after it.
			keptCommentDrafts.unsend(key);
			// The box showing this draft says why: the one that pressed, or one opened from the kept draft since it closed.
			// Never a box showing another draft: its reason would tell of the wrong post.
			commentDraft.update(value => value && value.key === key ? { ...value, error: message } : value);
			// The draft holds the toast, if one was shown, until its box comes on screen to say why or the draft is posted.
			keptCommentDrafts.toast(key, toastId);
		}, draft.selection ? liveFailureMessage : postFailureMessage, () => waits || keptCommentDrafts.onScreen(key)));
		// Posted: the text is the server's now, so no composer brings it back, whether this one is still open or not. Text
		// or tags changed since the press (in this composer, or one opened after it closed) stay, with a fresh id for their own post.
		const stored = keptCommentDrafts.posted(key, text, taggedAgentIds);
		// A composer this viewer opened from the draft while it posted, left unedited, shows what is stored now: it is that
		// draft, and ends as the one that pressed does.
		const that = (value: CommentDraft | null) => !!value && (value.identity === draft.identity || (stored && value.scope === draft.scope && opened.has(value.identity!)));
		const open = () => that(context.draft()) && context.scope() === draft.scope && context.isOpen();
		if (!open()) commentDraft.update(value => that(value) ? null : value);
		// A widget comment's mark is painted by the widget after placement, so only it holds the pending mark that long.
		if (open() && draft.selection && draft.holdUntilDrawn) posted = { id, selection: draft.selection };
		return open() ? id : null;
	}
	// A kept draft that knows its passage, or was pressed, opens in its box once its document is shown: on its passage when
	// that is found as kept (`findPlace`), sending if it was pressed; otherwise on no text, with why, and it is not sent until
	// the person selects where it goes. One typed on a passage that could not be told apart waits for a selection, as before.
	let resumed = '';
	$effect(() => {
		const key = keepKey(), kept = keptCommentDrafts.get(key);
		if (!key || resumed === key || !context.ready() || !kept?.body.trim() || !(kept.place || kept.send)) return;
		untrack(() => {
			// Already held as the one draft (open here or in another viewer of the document): it is not opened a second time.
			if (context.isOpen() || context.draft()?.key === key) return;
			const span = kept.place && findPlace(context.text(), kept.place);
			const offer = context.offerFor(span ? span[0] : 0, span ? span[1] : 0);
			if (!offer) return;
			resumed = key;
			if (span) select(offer);
			else { keptCommentDrafts.unsend(key); pending = { ...offer, text: kept.place?.quote ?? '' }; selected = undefined; }
			open(!span);
			if (span) unsentComments.open(key);
		});
	});
	return { get pending() { return pending; }, get reason() { return reason; }, get draft() { return scoped; }, get hasText() { return hasText; },
		get pendingComment() { return pendingComment; }, get keepKey() { return keepKey(); }, select, leave, start, cancel, setBody, submit };
}
