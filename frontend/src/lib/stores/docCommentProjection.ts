import { type ImageRegion, type ServerComment } from '$lib/api/docs';
import { relLabel } from '$lib/time';
import type { Placement } from './docCommentPlacement';

export type CommentAuthor = string;

export interface CommentReply {
	id?: string;
	author: CommentAuthor;
	authorKind?: 'user' | 'agent';
	text: string;
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
	at: string;
	createdAt?: string;
	resolved: boolean;
	orphaned?: boolean;
	acked?: boolean;
	review?: boolean;
	replacement?: string | null;
	decision?: 'accepted' | 'rejected' | null;
	decidedBy?: string | null;
	/** Replies by others newer than this viewer's seen stamp. The only copy of reply attention; read it through `hasNewReplies`. */
	newReplyIds?: string[];
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

/** Whether a thread has a reply this viewer has not seen: the one reading of `newReplyIds`. */
export function hasNewReplies(comment: Pick<DocComment, 'newReplyIds'>): boolean {
	return (comment.newReplyIds?.length ?? 0) > 0;
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
		at: relLabel(r.created_at),
		createdAt: r.created_at,
		resolved: !!r.resolved,
		orphaned: !!r.orphaned,
		acked: !!r.acked,
		review: !!r.review,
		replacement: r.replacement,
		decision: r.decision,
		decidedBy: r.decided_by,
		newReplyIds: r.new_reply_ids ?? [],
		replies: (r.replies ?? []).map((x) => ({ id: x.id, author: x.author, authorKind: x.author_kind, text: x.body, at: relLabel(x.created_at), createdAt: x.created_at })),
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
