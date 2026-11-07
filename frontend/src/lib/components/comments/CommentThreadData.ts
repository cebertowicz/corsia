import type { CommentReplyData } from './CommentReplyList.svelte';
import type { MentionOccurrence } from './mentionSelection';

export type CommentThreadData = {
	id: string;
	anchor: string;
	author: string;
	authorKind?: 'user' | 'agent';
	/** The authoring agent's thread, whose presence colour its avatar shares with its caret and marks. */
	authorThreadId?: string | null;
	text: string;
	taggedAgents?: { id: string; name: string }[];
	/** An agent asked the person here; the value is the person's own account name. */
	taggedPerson?: string | null;
	mentions?: MentionOccurrence[];
	at: string;
	createdAt?: string;
	resolved: boolean;
	orphaned?: boolean;
	/** The original text is gone from the document: the card says so, and keeps the old text in quotes. */
	detached?: boolean;
	/** An open suggestion edited inside: it keeps its place and cannot be accepted. */
	textChanged?: boolean;
	/** The person's own acknowledgement: it lights their Agree. */
	acked?: boolean;
	/** Everyone who has acknowledged the thread, in order: the Agreed line names them. */
	acknowledgedBy?: { id: string; name: string; kind: 'user' | 'agent' }[];
	replacement?: string | null;
	insertAfter?: boolean;
	decision?: 'accepted' | 'rejected' | null;
	decidedBy?: string | null;
	replies: CommentReplyData[];
	/** Replies by others newer than this viewer's seen stamp: the server's answer, held once in the store. */
	newReplyIds?: string[];
	newComment?: boolean;
	/** Live documents only (4.2): a row never `drawn` shows its placement on the card. */
	placement?: 'placed' | 'placing' | 'not_placed';
	drawn?: boolean;
	/** On part of an image (DCOL-FR13.4): its number there, and whether the image changed after it was pinned (FR13.9). */
	regionNumber?: number | null;
	regionStale?: boolean;
};
