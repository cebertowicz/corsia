import type { UndeliveredBatch } from './docFeedback';

export interface ServerComment {
	id: string;
	artifact_id: string;
	parent_id: string | null;
	author: string;
	author_kind: 'user' | 'agent';
	author_thread_id?: string | null;
	body: string;
	tagged_agent_ids?: string[];
	tagged_agents?: { id: string; name: string }[];
	/** An agent's ask for the person on this comment: their own account name, never an agent tag. */
	tagged_person?: string | null;
	tagged_agent_mentions?: import('$lib/components/comments/mentionSelection').MentionOccurrence[];
	anchor_text: string | null;
	from_off: number | null;
	to_off: number | null;
	review: number;
	resolved: number;
	/** Who has acknowledged the thread, in order; on a top-level comment only. `kind` user is the person. */
	acknowledged_by?: Acknowledger[];
	/** Agents the person's next reply wakes; on a top-level comment only. */
	reply_goes_to?: { id: string; name: string }[];
	replacement?: string | null;
	insert_after?: boolean | number;
	decision?: 'accepted' | 'rejected' | null;
	decided_by?: string | null;
	orphaned?: boolean;
	/** An open suggestion edited inside: it keeps its place and quote, and cannot be accepted. */
	text_changed?: boolean;
	created_at: string;
	updated_at: string;
	new_reply_ids?: string[];
	new_comment?: boolean;
	replies?: ServerComment[];
	/** A region comment on an image (DCOL-FR13.3): fractions of the image; w = h = 0 is a point. */
	region_x?: number | null;
	region_y?: number | null;
	region_w?: number | null;
	region_h?: number | null;
	/** Numbered per image in creation order, never reused. */
	region_number?: number | null;
	image_rev?: string | null;
	/** The image changed after this pin was placed. */
	region_stale?: boolean;
	/** For an image inside a document, the workspace file the image markdown shows; null on an image artifact. */
	image_src?: string | null;
}

export type Acknowledger = { id: string; name: string; kind: 'user' | 'agent' };

/** A box or point on an image, in fractions of its width and height; w = h = 0 is a point. */
export type ImageRegion = { x: number; y: number; w: number; h: number };

export interface CommentFeedback {
	open_count: number;
	/** Of the open comments, those held for the document's header Send and for its image viewer's Send. */
	document_held_count?: number;
	image_held_count?: number;
	owner_label: string | null;
	/** Who a new comment on the document goes to, by name: the agents delivery wakes. */
	goes_to?: { id: string; name: string }[];
	undelivered_batches: UndeliveredBatch[];
}

export interface CommentList {
	comments: ServerComment[];
	feedback: CommentFeedback;
}
