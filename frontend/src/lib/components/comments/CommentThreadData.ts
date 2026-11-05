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
	mentions?: MentionOccurrence[];
	at: string;
	createdAt?: string;
	resolved: boolean;
	orphaned?: boolean;
	acked?: boolean;
	replacement?: string | null;
	insertAfter?: boolean;
	decision?: 'accepted' | 'rejected' | null;
	decidedBy?: string | null;
	replies: CommentReplyData[];
	/** Replies by others newer than this viewer's seen stamp: the server's answer, held once in the store. */
	newReplyIds?: string[];
	/** Live documents only (4.2): a row never `drawn` shows its placement on the card. */
	placement?: 'placed' | 'placing' | 'not_placed';
	drawn?: boolean;
	/** On part of an image (DCOL-FR13.4): its number there, and whether the image changed after it was pinned (FR13.9). */
	regionNumber?: number | null;
	regionStale?: boolean;
};
