import { untrack } from 'svelte';
import { createMentionSelection, type MentionOccurrence } from '$lib/components/comments/mentionSelection';
import { commentDraft, cancelCommentCapture, discardCommentDraft, dropCommentDraft, setCommentBody, type CommentDraft } from '$lib/stores/commentDraft';
import { keptCommentDrafts } from '$lib/stores/keptCommentDrafts';
import { selectComment, selectionRange, SELECTION_LOST, SELECTION_RECONNECTED, type CommentSelection } from '$lib/stores/commentSelection';
import { postLive } from '$lib/stores/liveCommentPost';
import { forgetDraft, timed, timedPost } from '$lib/stores/commentPostTiming';
import { EARLIER_VERSION_POSTED } from '$lib/stores/docComments';
import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
import { addComment } from '$lib/stores/docComments';
import type { SelectionOffer } from '$lib/cm/widget-selection-types';
import { plainErrorText } from '$lib/plainError';
import { isNotCommentable, submitDocumentComment } from '$lib/docViewerOperations';
import type { CommentAnchor } from '$lib/docCommentSurfaceState.svelte';

export type ViewerSelection = SelectionOffer | null;
type Context = {
	scope: () => string;
	/** The document the draft is kept for, whichever viewer shows it (`documentDraftKey`). */
	keepKey: () => string;
	draft: () => CommentDraft | null;
	session: () => LiveDocumentSession | undefined;
	generation: () => number;
	text: () => string;
	isOpen: () => boolean;
	ensureArtifactId: () => Promise<string | null>;
	onFailure: (id: string, message: string) => void;
	onStart: (anchor: CommentAnchor) => void;
	onClose: () => void;
	/** True while a posted comment may still get its first drawn mark; false once it is drawn, cannot be placed (not_placed, orphaned) or is gone. */
	awaitsMark: (id: string) => boolean;
};
const aborted = () => new DOMException('Comment draft closed', 'AbortError');
/** What a failed post of a comment anchored in a live document says; an image's region comment there says the same. */
export function liveFailureMessage(error: unknown): string {
	if (!(error instanceof Error)) return 'Could not post the comment; your draft is kept.';
	if (error.message === SELECTION_LOST) return 'The selected text changed or was removed. Your comment draft is kept.';
	if (error.message.startsWith('409 ')) return 'The document changed before your comment could be attached. Your draft is kept. Try again.';
	// A timed-out post may already be committed, and its reservation is released: asking to try again invites a duplicate.
	if (error.name === 'PostTimeoutError') return "We couldn't confirm your comment was posted. Check the comments before posting again. Your draft is kept.";
	// A lost connection (fetch's own TypeError text) and a server error ("500 <body>") carry wire text, not words for the reader.
	if (error instanceof TypeError || /^5\d{2}\b/.test(error.message)) return "Couldn't post the comment — try again";
	if (/^\d{3}\b/.test(error.message)) return refusalMessage(error.message);
	return error.message;
}

/** A 4xx refusal ("<status> <body>" from `ok()`): the server's reason through the shared gate, otherwise a plain failure. */
function refusalMessage(wire: string): string {
	const [, status, body] = /^(\d{3})\s*([\s\S]*)$/.exec(wire)!;
	let detail: unknown;
	try { detail = JSON.parse(body)?.detail; } catch { /* A body that is not JSON has no reason to show. */ }
	if (detail !== undefined) console.error('[api] request rejected', Number(status), detail);
	const reason = plainErrorText(detail, '').replace(/\.+$/, '');
	return reason ? `Couldn't post the comment: ${reason}. Your draft is kept.` : "Couldn't post the comment. Your draft is kept.";
}

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
	let selected: CommentSelection | undefined;
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
	// A live tap in a widget shows its pending mark before Comment is pressed; selecting elsewhere clears it, and prose has none.
	// While the composer is open the scoped draft owns the mark: a new selection does not take it.
	// A held mark is never resolved against another session's Y doc.
	const held = $derived(posted && posted.selection.session === context.session() ? posted.selection : null);
	const pendingComment = $derived.by(() => {
		const selection = context.isOpen() ? scoped?.selection : pending?.inWidget ? selected : held;
		return selection?.session === context.session() ? selection : null;
	});
	$effect(() => { if (posted && !context.awaitsMark(posted.id)) posted = null; });
	/** Cancel, the one way to drop typed text. */
	function cancel(): void {
		posted = null;
		forgetDraft(keptCommentDrafts.get(context.keepKey())?.clientId);
		keptCommentDrafts.forget(context.keepKey()); discardCommentDraft(context.scope()); context.onClose();
	}
	/** What the composer holds, kept for this document until it posts or is cancelled. */
	function setBody(body: string): void {
		setCommentBody(body, context.scope());
		const draft = scoped;
		if (draft?.mentionSelection) keptCommentDrafts.keep(context.keepKey(), body, draft.mentionSelection);
	}
	function start(): void {
		if (!pending) return;
		cancelCommentCapture(context.scope());
		// The next composer on this document starts from what the last one held, its tags and why it did not post.
		const old = context.draft(), same = old?.scope === context.scope() ? old : null, kept = keptCommentDrafts.get(context.keepKey());
		const body = same?.body ?? kept?.body ?? '';
		const mentionSelection = same?.mentionSelection ?? kept?.mentionSelection ?? createMentionSelection(body);
		commentDraft.set({ from: pending.from, to: pending.to, text: pending.text, top: pending.top,
			body, mentionSelection, error: same ? same.error : kept?.error, label: typeof pending.label === 'function' ? pending.label() : pending.label, holdUntilDrawn: pending.inWidget, scope: context.scope(), selection: selected, identity: {}, sequence: 0 });
		const value = pending;
		pending = null; selected = undefined;
		context.onStart(value.anchor ?? new DOMRect(value.left, value.top, 0, 0));
	}
	async function submit(text: string, taggedAgentIds: string[] = [], threadId: string | null = null, mentions: MentionOccurrence[] = []): Promise<string | null> {
		const draft = context.draft();
		if (!draft || draft.scope !== context.scope()) return null;
		const key = context.keepKey(), tags = draft.mentionSelection ?? createMentionSelection(text);
		const current = () => context.draft()?.identity === draft.identity && context.scope() === draft.scope && context.isOpen();
		const mine = (update: (value: CommentDraft) => CommentDraft | null) => commentDraft.update(value => value && value.identity === draft.identity ? update(value) : value);
		keptCommentDrafts.clearError(key);
		mine(value => ({ ...value, error: undefined }));
		// Every press on this draft posts under its one id, so a press after an unanswered one never stores it twice.
		const press = keptCommentDrafts.claim(key, text, tags);
		const id = await timedPost(press.clientId, () => submitDocumentComment(async () => {
			if (draft.selection) return postLiveComment(draft, text, context, press, taggedAgentIds, threadId, mentions);
			const artifactId = await timed(press.clientId, 'ens', () => context.ensureArtifactId());
			if (!artifactId) throw new Error('Document artifact is unavailable.');
			return addComment(artifactId, draft.from, draft.to, draft.text, text, 'you', false, press.clientId, taggedAgentIds, threadId, mentions, press.again);
		}, (toastId, message) => {
			// Open or closed, the composer keeps the text with why it did not post, here and in the next composer on this document.
			// Text changed since an earlier version was stored goes out as a new comment on the next press.
			keptCommentDrafts.fail(key, text, tags, message, message === EARLIER_VERSION_POSTED);
			mine(value => ({ ...value, error: message }));
			context.onFailure(toastId, message);
		}, draft.selection ? liveFailureMessage : undefined));
		// Posted: the text is the server's now, so no composer brings it back, whether this one is still open or not.
		keptCommentDrafts.forget(key);
		if (!current()) mine(() => null);
		// A widget comment's mark is painted by the widget after placement, so only it holds the pending mark that long.
		if (current() && draft.selection && draft.holdUntilDrawn) posted = { id, selection: draft.selection };
		return current() ? id : null;
	}
	return { get pending() { return pending; }, get reason() { return reason; }, get draft() { return scoped; },
		get pendingComment() { return pendingComment; }, select, start, cancel, setBody, submit };
}
