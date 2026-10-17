import { untrack } from 'svelte';
import { commentDraft, cancelCommentCapture, discardCommentDraft, type CommentDraft } from '$lib/stores/commentDraft';
import { captureComment, selectComment, selectionRange, SELECTION_LOST, type CommentSelection } from '$lib/stores/commentSelection';
import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
import { addComment } from '$lib/stores/docComments';
import type { SelectionLabel } from '$lib/stores/commentDraft';
import { plainErrorText } from '$lib/plainError';
import { submitDocumentComment } from '$lib/docViewerOperations';
import type { CommentAnchor } from '$lib/docCommentSurfaceState.svelte';

export type ViewerSelection = { from: number; to: number; text: string; top: number; left: number; anchor?: () => DOMRect | null; label?: SelectionLabel; above?: boolean; inWidget?: boolean } | null;
type Context = {
	scope: () => string;
	draft: () => CommentDraft | null;
	session: () => LiveDocumentSession | undefined;
	generation: () => number;
	text: () => string;
	isOpen: () => boolean;
	ensureArtifactId: () => Promise<string | null>;
	onFailure: (id: string) => void;
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

async function postLiveComment(draft: CommentDraft, text: string, context: Context, current: () => boolean): Promise<string> {
	const selection = draft.selection!;
	cancelCommentCapture(draft.scope);
	const controller = new AbortController(), sequence = (draft.sequence ?? 0) + 1;
	commentDraft.update(value => value && value.identity === draft.identity ? { ...value, request: controller, sequence } : value);
	const captured = await captureComment(selection, sequence, controller.signal);
	const session = selection.session;
	try {
		if (!current() || controller.signal.aborted || context.session() !== session) throw aborted();
		return await addComment(session.artifactId, captured.from, captured.to, captured.quote, text, 'you', true, captured.clientId);
	} catch (error) {
		session.releaseReservation(captured.clientId);
		throw error;
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
		const session = context.session(), scope = context.scope();
		untrack(() => {
			const draft = context.draft();
			if (draft?.scope === scope && draft.selection && draft.selection.session !== session) discardCommentDraft(scope);
		});
		return () => { posted = null; discardCommentDraft(scope); };
	});
	const reason = $derived.by(() => {
		context.text(); context.generation();
		const draft = context.draft(), selection = draft?.scope === context.scope() ? draft.selection : undefined;
		return selection && !selectionRange(selection) ? SELECTION_LOST : '';
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
	const pendingComment = $derived((context.isOpen() ? scoped?.selection : pending?.inWidget ? selected : held) ?? null);
	$effect(() => { if (posted && !context.awaitsMark(posted.id)) posted = null; });
	function cancel(): void { posted = null; discardCommentDraft(context.scope()); context.onClose(); }
	function start(): void {
		if (!pending) return;
		cancelCommentCapture(context.scope());
		const old = context.draft(), body = old?.scope === context.scope() ? (old.body ?? '') : '';
		commentDraft.set({ from: pending.from, to: pending.to, text: pending.text, top: pending.top,
			body, label: typeof pending.label === 'function' ? pending.label() : pending.label, holdUntilDrawn: pending.inWidget, scope: context.scope(), selection: selected, identity: {}, sequence: 0 });
		const value = pending;
		pending = null; selected = undefined;
		context.onStart(value.anchor ?? new DOMRect(value.left, value.top, 0, 0));
	}
	async function submit(text: string): Promise<string | null> {
		const draft = context.draft();
		if (!draft || draft.scope !== context.scope()) return null;
		const current = () => context.draft()?.identity === draft.identity && context.scope() === draft.scope && context.isOpen();
		const id = await submitDocumentComment(async () => {
			if (draft.selection) return postLiveComment(draft, text, context, current);
			const artifactId = await context.ensureArtifactId();
			if (!current()) throw aborted();
			if (!artifactId) throw new Error('Document artifact is unavailable.');
			return addComment(artifactId, draft.from, draft.to, draft.text, text);
		}, context.onFailure, draft.selection ? liveFailureMessage : undefined);
		// A widget comment's mark is painted by the widget after placement, so only it holds the pending mark that long.
		if (current() && draft.selection && draft.holdUntilDrawn) posted = { id, selection: draft.selection };
		return current() ? id : null;
	}
	return { get pending() { return pending; }, get reason() { return reason; }, get draft() { return scoped; },
		get pendingComment() { return pendingComment; }, select, start, cancel, submit };
}
