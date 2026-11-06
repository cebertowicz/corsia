import { type ImageRegion, type ServerComment } from '$lib/api/docs';
import { relLabel } from '$lib/time';
import type { Placement } from './docCommentPlacement';

export type CommentAuthor = string;

export interface CommentReply {
	id?: string;
	author: CommentAuthor;
	authorKind?: 'user' | 'agent';
	text: string;
	taggedAgents?: { id: string; name: string }[];
	mentions?: import('$lib/components/comments/mentionSelection').MentionOccurrence[];
	at: string;
	createdAt?: string;
}

export interface DocComment {
	id: string;
	from?: number;
	to?: number;
	anchor: string;
	author: CommentAuthor;
	authorKind?: 'user' | 'agent';
	authorThreadId?: string | null;
	text: string;
	taggedAgents?: { id: string; name: string }[];
	mentions?: import('$lib/components/comments/mentionSelection').MentionOccurrence[];
	at: string;
	createdAt?: string;
	resolved: boolean;
	orphaned?: boolean;
	/** The original text is gone: nothing of the quote is left to mark (a collapsed range, or a quote found nowhere in the document). One rule for a comment and for a decided suggestion that keeps its replies. */
	detached?: boolean;
	/** An open suggestion whose text was edited inside: it keeps its place and its quote, and cannot be accepted. */
	textChanged?: boolean;
	/** When the thread last changed; a resolve sets it. The server keeps no separate resolve time, so this orders the resolved list. */
	updatedAt?: string;
	acked?: boolean;
	review?: boolean;
	replacement?: string | null;
	insertAfter?: boolean;
	decision?: 'accepted' | 'rejected' | null;
	decidedBy?: string | null;
	/** Unread reply IDs; `newComment` separately marks an unread top-level comment. */
	newReplyIds?: string[];
	newComment?: boolean;
	replies: CommentReply[];
	/** Live documents only (4.2); `drawn` once browser-made positions place it. */
	placement?: Placement;
	/** A user-requested live placement reload is queued or in flight. */
	placementReloading?: boolean;
	/** The latest user-requested live placement reload was refused. */
	placementReloadError?: boolean;
	drawn?: boolean;
	/** A comment on part of an image (DCOL-FR13.3); null for a text comment. */
	region?: ImageRegion | null;
	/** Its number on that image (FR13.4). */
	regionNumber?: number | null;
	/** The image changed after the pin was placed (FR13.9). */
	regionStale?: boolean;
	/** For an image inside a document, the workspace file it shows; null on an image artifact. */
	imageSrc?: string | null;
}

/** Whether a thread contains an unread top-level comment or reply. */
export function hasUnread(comment: Pick<DocComment, 'newReplyIds' | 'newComment'>): boolean {
	return !!comment.newComment || (comment.newReplyIds?.length ?? 0) > 0;
}

/**
 * The text a comment's place is found by, the server's rule (`comment_anchoring._anchored_quote`): an accepted
 * suggestion's replacement, which is what the document now holds there; otherwise its quote.
 */
export function anchoredQuote(comment: Pick<DocComment, 'anchor' | 'decision' | 'replacement'>): string {
	return comment.decision === 'accepted' && comment.replacement ? comment.replacement : comment.anchor;
}

export function fromServer(r: ServerComment): DocComment {
	return {
		id: r.id,
		from: r.from_off ?? undefined,
		to: r.to_off ?? undefined,
		anchor: r.anchor_text ?? '',
		author: r.author,
		authorKind: r.author_kind,
		authorThreadId: r.author_thread_id,
		text: r.body,
		taggedAgents: r.tagged_agents ?? [],
		mentions: r.tagged_agent_mentions ?? [],
		at: relLabel(r.created_at),
		createdAt: r.created_at,
		updatedAt: r.updated_at,
		resolved: !!r.resolved,
		orphaned: !!r.orphaned,
		textChanged: !!r.text_changed,
		acked: !!r.acked,
		review: !!r.review,
		replacement: r.replacement,
		insertAfter: !!r.insert_after,
		decision: r.decision,
		decidedBy: r.decided_by,
		newReplyIds: r.new_reply_ids ?? [],
		newComment: !!r.new_comment,
		replies: (r.replies ?? []).map((x) => ({ id: x.id, author: x.author, authorKind: x.author_kind, text: x.body, taggedAgents: x.tagged_agents ?? [], mentions: x.tagged_agent_mentions ?? [], at: relLabel(x.created_at), createdAt: x.created_at })),
		...regionFromServer(r)
	};
}

export function regionFromServer(r: ServerComment): Pick<DocComment, 'region' | 'regionNumber' | 'regionStale' | 'imageSrc'> {
	if (r.region_number == null || r.region_x == null || r.region_y == null) return {};
	return {
		region: { x: r.region_x, y: r.region_y, w: r.region_w ?? 0, h: r.region_h ?? 0 },
		regionNumber: r.region_number,
		regionStale: !!r.region_stale,
		imageSrc: r.image_src ?? null
	};
}
