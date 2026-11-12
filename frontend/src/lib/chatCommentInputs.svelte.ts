// Comment-only inputs: a posted comment, or a reply typed in a comment thread, with no chat text of its own.
// None is a chat message, so none enters the transcript's `messages` store (its gate hands them here). They
// live here, keyed by the comment they target: the text a box holds until the server stores it, then the
// server's row, whose delivery state that comment's thread shows. The frames answering an input settle it.
import { unknownSendFailure, type ChatCommentAnchor } from '$lib/chatCommentAnchors';
import { readMessageDeliveryState } from '$lib/messageDelivery';
import { plainErrorText } from '$lib/plainError';
import type { ReplyComment } from '$lib/stores/replyContextCapture';
import { mergeMessageDelivery, type Message } from '$lib/stores/threads';
import { on } from '$lib/ws/events';

/** A comment-only input: a posted comment or reply, with no chat text or attachment of its own. */
export function isCommentInput(row: Message): boolean {
	return row.role === 'user' && !row.content.trim() && !row.metadata?.attachments?.length
		&& !!row.metadata?.reply_context?.comments?.length;
}

type Draft = { text: string; inputId?: string; error?: string };
// A reply draft belongs to its comment; a new comment's draft to the passage it is on, so two passages of one message never share text, a failure or a post.
const replyKey = (commentId: string) => `reply:${commentId}`;
const rootKey = (threadId: string, messageId: string, anchor: Pick<ChatCommentAnchor, 'from_off' | 'to_off'>) => `root:${JSON.stringify([threadId, messageId, anchor.from_off, anchor.to_off])}`;
const drafts = $state<Record<string, Draft>>({});
// Every thread's comment-only inputs: this device's own from the moment they are sent (no delivery state until the server answers), and the server's rows.
let stored = $state.raw<Message[]>([]);

function absorb(rows: Message[]): void {
	let next = stored;
	for (const row of rows) {
		const inputId = row.metadata?.input_id;
		const at = next.findIndex(item => item.id === row.id || (!!inputId && item.metadata?.input_id === inputId));
		next = at < 0 ? [...next, row] : next.map((item, index) => index === at ? mergeMessageDelivery(item, row) : item);
		if (typeof inputId === 'string') settle(inputId, null);
	}
	stored = next;
}

/** The server answered this input: stored (null), so its row owns the text from here, or refused with a cause, which the draft keeps with the text. */
function settle(inputId: string, refusal: string | null): void {
	const key = Object.keys(drafts).find(item => drafts[item].inputId === inputId);
	// A refused input was never stored, so its own row goes; its text stays in the draft.
	if (refusal !== null) stored = stored.filter(row => row.delivery_state || row.metadata?.input_id !== inputId);
	if (!key) return;
	if (refusal === null) delete drafts[key];
	else drafts[key] = { ...drafts[key], inputId: undefined, error: refusal || unknownSendFailure(key.startsWith('reply:') ? 'reply' : 'comment') };
}

// The server stored this input (or changed its delivery): this socket hears it only by these frames, never by the row's broadcast.
function storedRow(frame: any, deliveryState: string): void {
	const inputId = frame?.input_id;
	if (typeof inputId !== 'string') return;
	// The server's copy may have reached this store first (a page load), under its message id and without the input id.
	const prior = stored.find(row => row.metadata?.input_id === inputId || (!!frame.message_id && row.id === frame.message_id));
	if (!prior || frame.thread_id !== prior.thread_id) return;
	absorb([{
		...prior,
		...(typeof frame.message_id === 'string' && frame.message_id ? { id: frame.message_id } : {}),
		...(typeof frame.created_at === 'string' && !prior.delivery_state ? { created_at: frame.created_at } : {}),
		metadata: { ...prior.metadata, input_id: inputId },
		delivery_state: deliveryState as Message['delivery_state'],
		delivery_error: deliveryState === 'queued' || deliveryState === 'delivered' ? null
			: typeof frame.delivery_error === 'string' && frame.delivery_error ? frame.delivery_error : plainErrorText(frame.message, 'Not delivered'),
		...(typeof frame.delivery_revision === 'number' ? { delivery_revision: frame.delivery_revision } : {}),
		...(typeof frame.seq === 'number' ? { seq: frame.seq } : {}),
	}]);
}

on('input_queued', frame => storedRow(frame, readMessageDeliveryState(frame?.delivery_state) ?? 'queued'));
on('input_accepted', frame => storedRow(frame, 'delivered'));
on('error', frame => {
	if (typeof frame?.input_id !== 'string') return;
	// A failure that carries the server's delivery state was stored first: its thread shows it with Retry.
	const state = readMessageDeliveryState(frame.delivery_state);
	if (state) storedRow(frame, state);
	else settle(frame.input_id, plainErrorText(frame.message, ''));
});

/** The transcript's gate: every row bound for `messages` passes here, and comment-only inputs come to this store instead. */
export function admitTranscriptRows(rows: Message[]): Message[] {
	if (!rows.some(isCommentInput)) return rows;
	absorb(rows.filter(isCommentInput));
	return rows.filter(row => !isCommentInput(row));
}

export const chatCommentInputs = {
	/** This chat's stored comment-only inputs. */
	rows(threadId: string | null | undefined): Message[] { return stored.filter(row => row.thread_id === threadId); },
	/** The comments an input of this chat carried, whether it is still on its way or stored. */
	comments(threadId: string, inputId?: string, messageId?: string): ReplyComment[] {
		const row = stored.find(item => item.thread_id === threadId && (item.id === messageId || (!!inputId && item.metadata?.input_id === inputId)));
		return row?.metadata?.reply_context?.comments ?? [];
	},
	/** The composer sent this comment as `inputId`; the server's answer to that input settles it here. */
	sending(inputId: string, threadId: string, comment: ReplyComment): void {
		stored = [...stored, { id: inputId, thread_id: threadId, role: 'user', content: '', created_at: new Date().toISOString(), metadata: { input_id: inputId, reply_context: { comments: [comment] } } }];
	},
	/** Whether a box's post is still waiting for the server. */
	posting(inputId: string): boolean { return Object.values(drafts).some(draft => draft.inputId === inputId); },
	get(commentId: string): string { return drafts[replyKey(commentId)]?.text ?? ''; },
	reply(commentId: string): Draft | undefined { return drafts[replyKey(commentId)]; },
	set(commentId: string, text: string): void { write(replyKey(commentId), text); },
	postReply(commentId: string, text: string, inputId: string | null, error: string): void { post(replyKey(commentId), text, inputId, error, 'reply'); },
	/** The draft the composer on this passage shows. */
	root(threadId: string, messageId: string, anchor: ChatCommentAnchor): Draft | undefined { return drafts[rootKey(threadId, messageId, anchor)]; },
	setRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string): void { write(rootKey(threadId, messageId, anchor), text); },
	/** Cancel drops the passage's text and failure; a post on its way keeps its draft until the server answers. */
	dropRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor): void {
		const key = rootKey(threadId, messageId, anchor);
		if (!drafts[key]?.inputId) delete drafts[key];
	},
	postRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string, inputId: string | null, error: string): void { post(rootKey(threadId, messageId, anchor), text, inputId, error, 'comment'); },
};

function write(key: string, text: string): void {
	const prior = drafts[key];
	if (text || prior?.inputId) drafts[key] = { ...prior, text };
	else delete drafts[key];
}

function post(key: string, text: string, inputId: string | null, error: string, noun: 'comment' | 'reply'): void {
	drafts[key] = { text, ...(inputId ? { inputId } : { error: error || unknownSendFailure(noun) }) };
}
