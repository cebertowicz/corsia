import type { UndeliveredBatch } from './docFeedback';

export interface ServerComment {
	id: string;
	artifact_id: string;
	parent_id: string | null;
	author: string;
	author_kind: 'user' | 'agent';
	author_thread_id?: string | null;
	body: string;
	anchor_text: string | null;
	from_off: number | null;
	to_off: number | null;
	review: number;
	resolved: number;
	acked: number;
	replacement?: string | null;
	decision?: 'accepted' | 'rejected' | null;
	decided_by?: string | null;
	orphaned?: boolean;
	created_at: string;
	updated_at: string;
	reply_waiting?: boolean;
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
}

/** A box or point on an image, in fractions of its width and height; w = h = 0 is a point. */
export type ImageRegion = { x: number; y: number; w: number; h: number };

export interface CommentFeedback {
	open_count: number;
	owner_label: string | null;
	grace_closes_at: string | null;
	undelivered_batches: UndeliveredBatch[];
}

export interface CommentList {
	comments: ServerComment[];
	feedback: CommentFeedback;
}
