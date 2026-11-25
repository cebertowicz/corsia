// The text typed in a chat comment's box or a comment thread's reply field, until the server stores it.
// A stored comment or reply lives on the server, on the comment it belongs to, with its own delivery state;
// nothing here copies it. The text itself is kept by `keptCommentDrafts`, as a document's is: on this device under the
// signed-in person, so a reload, a quit and an update restart find it, with the draft's one client id, which every press posts
// under. What stays here is only what the page knows while a post is on its way or refused: that it is posting, and the cause
// the box shows. A reload never restores a failure.
import { CommentSendRefused, refusalText, type ChatCommentAnchor } from '$lib/chatCommentAnchors';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { CHAT_REPLY_SCOPE, chatDraftKey, keptCommentDrafts, replyDraftKey } from '$lib/stores/keptCommentDrafts.svelte';

/** `final`: the failure is one no press can get past (its chat is deleted), so the box offers no Retry. */
type Status = { posting?: boolean; error?: string; final?: boolean };
type Draft = Status & { text: string };
// A reply draft belongs to its comment; a new comment's draft to the passage it is on, so two passages of one message never share text, a failure or a post.
const replyKey = (commentId: string) => replyDraftKey(CHAT_REPLY_SCOPE, commentId);
const rootKey = (threadId: string, messageId: string, anchor: Pick<ChatCommentAnchor, 'from_off' | 'to_off'>) => chatDraftKey(threadId, messageId, anchor);
const status = $state<Record<string, Status>>({});

export const chatCommentDrafts = {
	/** Whether any box is waiting for the server; a highlight shows its comment's reply as sending meanwhile. */
	posting(): boolean { return Object.values(status).some(state => state.posting); },
	get(commentId: string): string { return keptCommentDrafts.get(replyKey(commentId))?.body ?? ''; },
	reply(commentId: string): Draft | undefined { return read(replyKey(commentId)); },
	set(commentId: string, text: string): void { write(replyKey(commentId), text); },
	/** The key the reply's field shows its draft under (`keptCommentDrafts.shown`). */
	replyKey,
	/** Post a reply typed in this comment's thread; true once the server stored it. `send` gets the draft's client id. */
	postReply(commentId: string, text: string, send: (inputId: string) => Promise<void>): Promise<boolean> { return post(replyKey(commentId), text, 'reply', send); },
	/** The draft the composer on this passage shows. */
	root(threadId: string, messageId: string, anchor: ChatCommentAnchor): Draft | undefined { return read(rootKey(threadId, messageId, anchor)); },
	setRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string): void { write(rootKey(threadId, messageId, anchor), text); },
	/** The key the composer on this passage shows its draft under. */
	rootKey,
	/** Cancel drops the passage's text and failure; a post on its way keeps its draft until the server answers. */
	dropRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor): void {
		const key = rootKey(threadId, messageId, anchor);
		if (status[key]?.posting) return;
		keptCommentDrafts.forget(key);
		delete status[key];
	},
	/** Post a new comment on this passage; true once the server stored it. `send` gets the draft's client id. */
	postRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string, send: (inputId: string) => Promise<void>): Promise<boolean> { return post(rootKey(threadId, messageId, anchor), text, 'comment', send); },
};

/** The kept text with what the page knows of its post. A failure of a draft that is gone (its Sign out) is not shown. */
function read(key: string): Draft | undefined {
	const text = keptCommentDrafts.get(key)?.body ?? '';
	const state = status[key];
	return text || state?.posting ? { text, ...state } : undefined;
}

function write(key: string, text: string): void {
	const prior = keptCommentDrafts.get(key);
	if (!text && !status[key]?.posting) delete status[key];
	if (!text) return keptCommentDrafts.keepReply(key, text, createMentionSelection(text));
	// Edited since a press: the earlier version may be stored under the draft's id already, so what is typed now posts as its own comment, under a fresh id.
	if (prior?.clientId && prior.body !== text) keptCommentDrafts.fail(key, text, createMentionSelection(text), '', true);
	else keptCommentDrafts.keepReply(key, text, createMentionSelection(text));
}

/** The box is read-only while it posts. Stored, the text is the server's and the draft goes; refused, the text stays with its cause. */
async function post(key: string, text: string, noun: 'comment' | 'reply', send: (inputId: string) => Promise<void>): Promise<boolean> {
	const kept = keptCommentDrafts.get(key);
	const press = keptCommentDrafts.claim(key, kept?.body ?? text, kept?.mentionSelection ?? createMentionSelection(text));
	status[key] = { posting: true };
	try {
		await send(press.clientId);
		keptCommentDrafts.forget(key);
		delete status[key];
		return true;
	} catch (failure) {
		status[key] = { error: refusalText(failure, noun), ...(failure instanceof CommentSendRefused && failure.final ? { final: true } : {}) };
		return false;
	}
}
