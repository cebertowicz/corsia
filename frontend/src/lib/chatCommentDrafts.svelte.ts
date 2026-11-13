// The text typed in a chat comment's box or a comment thread's reply field, until the server stores it.
// A stored comment or reply lives on the server, on the comment it belongs to, with its own delivery state;
// nothing here copies it. A post that fails keeps its text here with the cause the box shows.
import { CommentSendRefused, refusalText, type ChatCommentAnchor } from '$lib/chatCommentAnchors';

/** `final`: the failure is one no press can get past (its chat is deleted), so the box offers no Retry. */
type Draft = { text: string; posting?: boolean; error?: string; final?: boolean };
// A reply draft belongs to its comment; a new comment's draft to the passage it is on, so two passages of one message never share text, a failure or a post.
const replyKey = (commentId: string) => `reply:${commentId}`;
const rootKey = (threadId: string, messageId: string, anchor: Pick<ChatCommentAnchor, 'from_off' | 'to_off'>) => `root:${JSON.stringify([threadId, messageId, anchor.from_off, anchor.to_off])}`;
const drafts = $state<Record<string, Draft>>({});

export const chatCommentDrafts = {
	/** Whether any box is waiting for the server; a highlight shows its comment's reply as sending meanwhile. */
	posting(): boolean { return Object.values(drafts).some(draft => draft.posting); },
	get(commentId: string): string { return drafts[replyKey(commentId)]?.text ?? ''; },
	reply(commentId: string): Draft | undefined { return drafts[replyKey(commentId)]; },
	set(commentId: string, text: string): void { write(replyKey(commentId), text); },
	/** Post a reply typed in this comment's thread; true once the server stored it. */
	postReply(commentId: string, text: string, send: () => Promise<void>): Promise<boolean> { return post(replyKey(commentId), text, 'reply', send); },
	/** The draft the composer on this passage shows. */
	root(threadId: string, messageId: string, anchor: ChatCommentAnchor): Draft | undefined { return drafts[rootKey(threadId, messageId, anchor)]; },
	setRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string): void { write(rootKey(threadId, messageId, anchor), text); },
	/** Cancel drops the passage's text and failure; a post on its way keeps its draft until the server answers. */
	dropRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor): void {
		const key = rootKey(threadId, messageId, anchor);
		if (!drafts[key]?.posting) delete drafts[key];
	},
	/** Post a new comment on this passage; true once the server stored it. */
	postRoot(threadId: string, messageId: string, anchor: ChatCommentAnchor, text: string, send: () => Promise<void>): Promise<boolean> { return post(rootKey(threadId, messageId, anchor), text, 'comment', send); },
};

function write(key: string, text: string): void {
	const prior = drafts[key];
	if (text || prior?.posting) drafts[key] = { ...prior, text };
	else delete drafts[key];
}

/** The box is read-only while it posts. Stored, the text is the server's and the draft goes; refused, the text stays with its cause. */
async function post(key: string, text: string, noun: 'comment' | 'reply', send: () => Promise<void>): Promise<boolean> {
	drafts[key] = { text, posting: true };
	try {
		await send();
		delete drafts[key];
		return true;
	} catch (failure) {
		drafts[key] = { text, error: refusalText(failure, noun), ...(failure instanceof CommentSendRefused && failure.final ? { final: true } : {}) };
		return false;
	}
}
