// Whether the comment stored under a new comment's draft id is the version a press of Comment sent, compared the way
// the server stores it. A post finds that stored comment two ways (the server's client_id dedupe answering a POST, and
// the lookup after a POST that got no answer); both ask `checkVersion`.
import type { postComment, ServerComment } from '$lib/api/docs';

export type PostFields = Parameters<typeof postComment>[1];
/** What a press of Comment sent that a stored comment must hold to be that press's. */
export type DraftVersion = Pick<PostFields, 'body' | 'tagged_agent_ids'>;

/** What a press of Comment asked for: the stored comment under the draft's id was posted from other text or tags. */
export const EARLIER_VERSION_POSTED = 'Your earlier version was posted. Your changes are kept here.';
export class EarlierVersionPosted extends Error {
	constructor(readonly row: ServerComment) {
		super(EARLIER_VERSION_POSTED);
		this.name = 'EarlierVersionPosted';
	}
}

/** Python's `str.strip()`, which the server stores a new comment's body through: its whitespace, not JavaScript's `trim`. */
const SERVER_STRIP = /^[\t\n\v\f\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\t\n\v\f\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/g;

/**
 * Whether the comment stored under a draft's id holds what this press sent, compared as the server stores it: the body
 * stripped (api/doc_comments.py create_comment, `body.body.strip()`) and the tags as a set (comment_recipients.save_tags).
 * Both ways a post finds its stored comment, the server's client_id dedupe and the lookup after a failed POST, ask this.
 */
export function checkVersion(row: ServerComment, c: DraftVersion): ServerComment {
	const tags = (ids: string[]) => [...new Set(ids)].sort().join('\n');
	const stored = row.tagged_agent_ids ?? row.tagged_agents?.map((tag) => tag.id) ?? [];
	if (row.body === c.body.replace(SERVER_STRIP, '') && tags(stored) === tags(c.tagged_agent_ids ?? [])) return row;
	throw new EarlierVersionPosted(row);
}
