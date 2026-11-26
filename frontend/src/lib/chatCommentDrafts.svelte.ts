// The text typed in a chat comment's box or a comment thread's reply field, until the server stores it.
// A stored comment or reply lives on the server, on the comment it belongs to, with its own delivery state;
// nothing here copies it. The text itself is kept by `keptCommentDrafts`, as a document's is: on this device under the
// signed-in person, so a reload, a quit and an update restart find it, with the draft's one client id, which every press posts
// under. What stays here is only what the page knows while a post is on its way or refused: that it is posting, and the cause
// the box shows. A reload never restores a failure. From a press, the draft keeps where it goes (`ChatSend`), so a post that
// fails for a reason that passes goes again on its own (`unsentComments`) and the box reads "Sending…" meanwhile.
import { get } from 'svelte/store';
import { SvelteSet } from 'svelte/reactivity';
import { threads } from '$lib/stores/threads';
import { CommentSendRefused, refusalText, type ChatCommentAnchor } from '$lib/chatCommentAnchors';
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import { CHAT_REPLY_SCOPE, chatDraftKey, keptCommentDrafts, replyDraftKey } from '$lib/stores/keptCommentDrafts.svelte';
import type { ChatSend } from '$lib/stores/keptCommentDraftsStorage';
import { placeOf, type KeptPlace } from '$lib/keptPlace';

/** `final`: the failure is one no press can get past (its chat is deleted), so the box offers no Retry. */
type Status = { posting?: boolean; error?: string; final?: boolean };
type Draft = Status & { text: string };
// A reply draft belongs to its comment; a new comment's draft to the passage it is on, so two passages of one message never share text, a failure or a post.
const replyKey = (commentId: string) => replyDraftKey(CHAT_REPLY_SCOPE, commentId);
const rootKey = (threadId: string, messageId: string, anchor: Pick<ChatCommentAnchor, 'from_off' | 'to_off'>) => chatDraftKey(threadId, messageId, anchor);
const status = $state<Record<string, Status>>({});
/** Drafts that posted on their own (`unsentComments`) while their box may be open: that box closes on the posted comment. */
const wentOnTheirOwn = new SvelteSet<string>();
/** Where a press posts to, and what a notice calls it. */
export type ChatPress = { chat: ChatSend; name?: string };

/** What a press on this passage keeps so it can go on its own: where it goes, and its chat's title for a notice. */
export function chatPress(chat: ChatSend): ChatPress {
	const title = get(threads).find((thread) => thread.id === chat.threadId)?.title;
	return { chat, ...(title ? { name: title } : {}) };
}

export const chatCommentDrafts = {
	/** Whether any box is waiting for the server; a highlight shows its comment's reply as sending meanwhile. */
	posting(): boolean { return Object.values(status).some(state => state.posting); },
	get(commentId: string): string { return keptCommentDrafts.get(replyKey(commentId))?.body ?? ''; },
	reply(commentId: string): Draft | undefined { return read(replyKey(commentId)); },
	set(commentId: string, text: string): void { write(replyKey(commentId), text); },
	/** The key the reply's field shows its draft under (`keptCommentDrafts.shown`). */
	replyKey,
	/**
	 * Post a reply typed in this comment's thread; true once the server stored it. `send` gets the draft's client id. With
	 * `press`, the reply goes on its own if this post fails for a reason that passes.
	 */
	postReply(commentId: string, text: string, send: (inputId: string) => Promise<void>, press?: ChatPress): Promise<boolean> { return post(replyKey(commentId), text, 'reply', send, press); },
	/** The draft the composer on this passage shows. */
	root(threadId: string, messageId: string, anchor: ChatCommentAnchor): Draft | undefined { return read(rootKey(threadId, messageId, anchor)); },
	/** The composer's text on this passage, kept with the passage (`placeOf` its message's text), so it comes back on it. */
	setRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string): void {
		write(rootKey(threadId, messageId, anchor), text, placeOf(anchor.anchor_prefix + anchor.quote + anchor.anchor_suffix, anchor.from_off, anchor.to_off, anchor.quote));
	},
	/** The key the composer on this passage shows its draft under. */
	rootKey,
	/** Cancel drops the passage's text and failure; a post on its way keeps its draft until the server answers. */
	dropRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor): void {
		const key = rootKey(threadId, messageId, anchor);
		if (status[key]?.posting) return;
		keptCommentDrafts.forget(key);
		delete status[key];
	},
	/** Post a new comment on this passage; true once the server stored it. `send` gets the draft's client id. `press` as for a reply. */
	postRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string, send: (inputId: string) => Promise<void>, press?: ChatPress): Promise<boolean> { return post(rootKey(threadId, messageId, anchor), text, 'comment', send, press); },
	/** It was refused when it went on its own: its box says why, and drops Retry if `final`. */
	refused(key: string, error: string, final: boolean): void { status[key] = { error, ...(final ? { final: true } : {}) }; },
	/** It posted on its own: an open box on it closes (`postedOnItsOwn`). */
	wentOnItsOwn(key: string): void { delete status[key]; wentOnTheirOwn.add(key); },
	/** Whether the draft under `key` posted on its own and no box has closed on it since (`closedOn`). */
	postedOnItsOwn(key: string): boolean { return wentOnTheirOwn.has(key); },
	/** The box on the draft under `key` closed on its post, or opened on new text there. */
	closedOn(key: string): void { wentOnTheirOwn.delete(key); },
};

/** The kept text with what the page knows of its post. A failure of a draft that is gone (its Sign out) is not shown. */
function read(key: string): Draft | undefined {
	const text = keptCommentDrafts.get(key)?.body ?? '';
	const state = status[key];
	return text || state?.posting ? { text, ...state } : undefined;
}

function write(key: string, text: string, place?: KeptPlace): void {
	const prior = keptCommentDrafts.get(key);
	if (!text && !status[key]?.posting) delete status[key];
	if (!text) return keptCommentDrafts.keepReply(key, text, createMentionSelection(text));
	// Edited since a press: the earlier version may be stored under the draft's id already, so what is typed now posts as its own comment, under a fresh id.
	if (prior?.clientId && prior.body !== text) keptCommentDrafts.fail(key, text, createMentionSelection(text), '', true);
	else keptCommentDrafts.keepReply(key, text, createMentionSelection(text));
	if (place && !keptCommentDrafts.get(key)?.place) keptCommentDrafts.keep(key, text, keptCommentDrafts.get(key)!.mentionSelection, place);
}

/**
 * The box is read-only while it posts. Stored, the text is the server's and the draft goes. A failure that passes leaves a
 * pressed draft to go on its own, its box reading "Sending…"; a refusal keeps the text with its cause, and it waits for the
 * person's next press.
 */
async function post(key: string, text: string, noun: 'comment' | 'reply', send: (inputId: string) => Promise<void>, where?: ChatPress): Promise<boolean> {
	const kept = keptCommentDrafts.get(key);
	const press = keptCommentDrafts.claim(key, kept?.body ?? text, kept?.mentionSelection ?? createMentionSelection(text), undefined, where);
	status[key] = { posting: true };
	try {
		await send(press.clientId);
		keptCommentDrafts.forget(key);
		delete status[key];
		return true;
	} catch (failure) {
		const passes = !(failure instanceof CommentSendRefused) || failure.passes;
		if (passes && keptCommentDrafts.get(key)?.send) {
			delete status[key];
			return false;
		}
		keptCommentDrafts.unsend(key);
		status[key] = { error: refusalText(failure, noun), ...(failure instanceof CommentSendRefused && failure.final ? { final: true } : {}) };
		return false;
	}
}
