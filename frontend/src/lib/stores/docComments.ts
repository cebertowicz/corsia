import { writable, get } from 'svelte/store';
import {
	fetchComments, markCommentThreadViewed, postComment, patchComment, persistPositions, type ImageRegion, type ServerComment
} from '$lib/api/docs';
import { relLabel } from '$lib/time';
import { trackPlacements, type Placement } from './docCommentPlacement';
import { withhold } from './docCommentHolds';
import { newClientId } from './documentWire';


function findById(rows: ServerComment[], id: string): ServerComment | null {
	for (const r of rows) {
		if (r.id === id) return r;
		const reply = (r.replies ?? []).find((x) => x.id === id);
		if (reply) return reply;
	}
	return null;
}

async function postReconciled(
	artifactId: string,
	clientId: string,
	c: Parameters<typeof postComment>[1]
): Promise<ServerComment> {
	try {
		return await postComment(artifactId, { ...c, client_id: clientId });
	} catch (e) {
		const result = await fetchComments(artifactId).catch(() => null);
		const found = result && findById(result.comments, clientId);
		if (found) return found;
		throw e;
	}
}

export type CommentAuthor = string;

export interface CommentReply {
	id?: string;
	author: CommentAuthor;
	authorKind?: 'user' | 'agent';
	text: string;
	at: string;
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
	replyWaiting?: boolean;
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
}

export const docComments = writable<Record<string, DocComment[]>>({});
/** `owner` is the document owner's name from the last load; null when it has no owner thread, so a re-open reaches nobody. */
export type CommentLoad = { state: 'loading' | 'ready' | 'error'; error?: string; owner?: string | null };
export const commentLoads = writable<Record<string, CommentLoad>>({});
const placements = trackPlacements(docComments);
/** Retry a live row's placement: one new positions request for its document. */
export const retryPlacement = (commentId: string) => placements.retry(commentId);
/** Re-resolve orphaned live placements without reloading or changing document text. */
export const reloadPlacements = (artifactId: string) => placements.reload(artifactId);

/**
 * The one way a lane hold changes what is shown. The document's current rows, edited, go through the placement
 * owner exactly as a load's do, so a comment that becomes visible is placed and asked for like any other.
 * Comments still held stay out.
 */
export function reshow(artifactId: string, edit: (rows: DocComment[]) => DocComment[] = (rows) => rows): void {
	placements.show(artifactId, withhold(artifactId, edit(get(docComments)[artifactId] ?? [])));
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
		replyWaiting: !!r.reply_waiting,
		replies: (r.replies ?? []).map((x) => ({ id: x.id, author: x.author, authorKind: x.author_kind, text: x.body, at: relLabel(x.created_at) })),
		...regionFromServer(r)
	};
}

function regionFromServer(r: ServerComment): Pick<DocComment, 'region' | 'regionNumber' | 'regionStale'> {
	if (r.region_number == null || r.region_x == null || r.region_y == null) return {};
	return {
		region: { x: r.region_x, y: r.region_y, w: r.region_w ?? 0, h: r.region_h ?? 0 },
		regionNumber: r.region_number,
		regionStale: !!r.region_stale
	};
}

type CommentFeedback = Awaited<ReturnType<typeof fetchComments>>['feedback'];
/** Per document: the load running now, and the one load every caller that arrives meanwhile shares. */
const runningLoad = new Map<string, Promise<CommentFeedback>>();
const followingLoad = new Map<string, Promise<CommentFeedback>>();

/**
 * Load a document's comments. Loads of one document never overlap: a burst of
 * change frames costs the load already running plus one more, which starts
 * after it and so reads everything the burst wrote.
 */
export function loadComments(artifactId: string): Promise<CommentFeedback> {
	const queued = followingLoad.get(artifactId);
	if (queued) return queued;
	const running = runningLoad.get(artifactId);
	if (!running) return startLoad(artifactId);
	const following = running.catch(() => undefined).then(() => {
		followingLoad.delete(artifactId);
		return startLoad(artifactId);
	});
	followingLoad.set(artifactId, following);
	return following;
}

function startLoad(artifactId: string): Promise<CommentFeedback> {
	const load = fetchAndShow(artifactId).finally(() => {
		if (runningLoad.get(artifactId) === load) runningLoad.delete(artifactId);
	});
	runningLoad.set(artifactId, load);
	return load;
}

async function fetchAndShow(artifactId: string): Promise<CommentFeedback> {
	// A refresh of comments already shown (a change frame from any canvas or
	// agent) stays 'ready': only a first load, or a retry after an error, reads
	// as loading and disables the comments control.
	if (get(commentLoads)[artifactId]?.state !== 'ready') {
		commentLoads.update((all) => ({ ...all, [artifactId]: { state: 'loading' } }));
	}
	try {
		const { comments, feedback } = await fetchComments(artifactId);
		placements.show(artifactId, withhold(artifactId, comments.map(fromServer)));
		commentLoads.update((all) => ({ ...all, [artifactId]: { state: 'ready', owner: feedback.owner_label } }));
		return feedback;
	} catch (error) {
		commentLoads.update((all) => ({ ...all, [artifactId]: { state: 'error', error: error instanceof Error ? error.message : String(error) } }));
		throw error;
	}
}

function isAgent(author: CommentAuthor): boolean {
	return author !== 'you';
}

function upsertTop(artifactId: string, row: ServerComment) {
	docComments.update((all) => {
		const list = all[artifactId] ? [...all[artifactId]] : [];
		if (list.some((c) => c.id === row.id)) return all;
		list.unshift(fromServer(row));
		return { ...all, [artifactId]: list };
	});
}

export async function addComment(artifactId: string, from: number, to: number, anchor: string, text: string, author: CommentAuthor = 'you', liveCapture = false, clientId?: string): Promise<string> {
	const cid = clientId ?? newClientId();
	const row = await postReconciled(artifactId, cid, {
		body: text, anchor_text: anchor, from_off: from, to_off: to, ...(liveCapture ? { live_capture: true } : {}),
		author, author_kind: isAgent(author) ? 'agent' : 'user'
	});
	upsertTop(artifactId, row);
	return row.id;
}


/** Comment on part of an image artifact: a box, or a point when its width and height are 0. */
export async function addRegionComment(artifactId: string, region: ImageRegion, text: string): Promise<string> {
	const row = await postReconciled(artifactId, newClientId(), { body: text, region, author: 'you', author_kind: 'user' });
	upsertTop(artifactId, row);
	return row.id;
}

/** Move a pin. The server places it on the image as it is now, so it is no longer stale. */
export async function moveRegion(artifactId: string, commentId: string, region: ImageRegion): Promise<void> {
	const row = await patchComment(commentId, { region });
	docComments.update((all) => ({
		...all,
		[artifactId]: (all[artifactId] ?? []).map((c) => (c.id === commentId ? { ...c, ...regionFromServer(row) } : c))
	}));
}

export async function addReply(artifactId: string, commentId: string, text: string, author: CommentAuthor = 'you') {
	const cid = newClientId();
	const row = await postReconciled(artifactId, cid, {
		body: text, parent_id: commentId, author, author_kind: isAgent(author) ? 'agent' : 'user'
	});
	docComments.update((all) => {
		const list = (all[artifactId] ?? []).map((c) => {
			if (c.id !== commentId) return c;
			if (c.replies.some((r) => r.id === row.id)) return c;
			return { ...c, replies: [...c.replies, { id: row.id, author, text, at: 'just now' }] };
		});
		return { ...all, [artifactId]: list };
	});
}

function patchFlag(artifactId: string, commentId: string, field: 'resolved' | 'acked', value: boolean) {
	let prev: boolean | undefined;
	docComments.update((all) => {
		const list = (all[artifactId] ?? []).map((c) => {
			if (c.id !== commentId) return c;
			prev = !!c[field];
			return { ...c, [field]: value };
		});
		return { ...all, [artifactId]: list };
	});
	patchComment(commentId, { [field]: value }).catch((e) => {
		console.error(`comment ${field} failed`, e);
		docComments.update((all) => {
			const list = (all[artifactId] ?? []).map((c) => (c.id === commentId ? { ...c, [field]: prev } : c));
			return { ...all, [artifactId]: list };
		});
	});
}

export async function markThreadViewed(artifactId: string, rootId: string, seenReplyId: string): Promise<void> {
	const result = await markCommentThreadViewed(artifactId, rootId, seenReplyId);
	docComments.update((all) => ({
		...all,
		[artifactId]: (all[artifactId] ?? []).map((comment) => (
			comment.id === result.root_id ? { ...comment, replyWaiting: result.reply_waiting } : comment
		)),
	}));
}

export function resolveComment(artifactId: string, commentId: string, resolved = true) {
	patchFlag(artifactId, commentId, 'resolved', resolved);
}

export function ackComment(artifactId: string, commentId: string, acked = true) {
	patchFlag(artifactId, commentId, 'acked', acked);
}

const dirty = new Map<string, Set<string>>();

export function remapComments(artifactId: string, mapPos: (pos: number) => number) {
	docComments.update((all) => {
		const list = (all[artifactId] ?? []).map((c) => {
			// A live row moves only by its own positions, and the server alone persists its offsets.
			if (c.drawn || c.placement || c.from == null || c.to == null) return c;
			const next = { ...c, from: mapPos(c.from), to: mapPos(c.to) };
			if (next.from !== c.from || next.to !== c.to) {
				if (!dirty.has(artifactId)) dirty.set(artifactId, new Set());
				dirty.get(artifactId)!.add(c.id);
			}
			return next;
		});
		return { ...all, [artifactId]: list };
	});
}

export async function flushPositions(artifactId: string, fullText?: string) {
	const ids = dirty.get(artifactId);
	if (!ids?.size) return;
	const list = get(docComments)[artifactId] ?? [];
	const updates = list
		.filter((c) => ids.has(c.id) && c.from != null && c.to != null)
		// A restored unsaved draft can temporarily carry offsets resolved against
		// the on-disk revision. Never persist a failed exact-slice match: the
		// server remains the single anchor resolver on the next read.
		.filter((c) => fullText === undefined || rangeFor(c, fullText) !== null)
		.map((c) => ({ id: c.id, from_off: c.from!, to_off: c.to! }));
	dirty.delete(artifactId);
	if (updates.length) await persistPositions(artifactId, updates).catch((e) => console.error('persist positions failed', e));
}

export function rangeFor(c: DocComment, fullText: string): { from: number; to: number } | null {
	if (
		c.from != null && c.to != null && c.to > c.from && c.to <= fullText.length
		&& fullText.slice(c.from, c.to) === c.anchor
	) return { from: c.from, to: c.to };
	return null;
}
