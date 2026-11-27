import { writable, get } from 'svelte/store';
import {
	fetchComments, markCommentThreadViewed, patchComment, persistPositions, type ServerComment
} from '$lib/api/docs';
import { trackPlacements } from './docCommentPlacement';
import { anchoredQuote, fromServer, type CommentAuthor, type DocComment } from './docCommentProjection';
import { copyStamp, heldIds, shownStamp, withhold } from './docCommentHolds';
import { newClientId } from './documentWire';
import { EarlierVersionPosted, type DraftVersion, type PostFields } from './commentVersion';
import { findPosted, postReconciled } from './commentReconcile';
import type { ChangeDesc } from '@codemirror/state';
import { mapCommentRange } from '$lib/cm/comment-range';

export { fromServer, hasUnread, regionFromServer } from './docCommentProjection';
export type { CommentAuthor, CommentReply, DocComment } from './docCommentProjection';
export { EARLIER_VERSION_POSTED, EarlierVersionPosted, type DraftVersion } from './commentVersion';

/**
 * A new comment's stored row, shown. When the comment stored under the draft's id was posted from other text or tags,
 * that one shows and this fails with `EarlierVersionPosted`, so the draft keeps the changes.
 */
async function showing(artifactId: string, find: () => Promise<ServerComment | null>): Promise<string | null> {
	try {
		const row = await find();
		if (row) upsertTop(artifactId, row);
		return row?.id ?? null;
	} catch (error) {
		if (error instanceof EarlierVersionPosted) upsertTop(artifactId, error.row);
		throw error;
	}
}

/** Post a new comment under its draft's id and show it (`showing`). */
export async function postTop(artifactId: string, clientId: string, c: PostFields, again = false): Promise<string> {
	return (await showing(artifactId, () => postReconciled(artifactId, clientId, c, again)))!;
}

/** Show the comment an earlier press of Comment stored under `clientId`, if one did (`showing`); null when none is found. */
export function showPosted(artifactId: string, clientId: string, c: DraftVersion): Promise<string | null> {
	return showing(artifactId, () => findPosted(artifactId, clientId, c, true));
}


export const docComments = writable<Record<string, DocComment[]>>({});
/**
 * Threads the person just picked to read (a click or tap on a highlight, a marker, the jump control, an image tag); the
 * card that shows one stamps it read and removes it. Each press sets a new value, so pressing an already-picked thread
 * reads what arrived since. A document thread is keyed by its id, a chat margin card by `chatPickKey`.
 */
export const pickedComments = writable<Set<string>>(new Set());
/** A chat comment's key in `pickedComments`, kept apart from document comment ids. */
export const chatPickKey = (commentId: string) => `chat:${commentId}`;
/** `owner` is the document owner's name from the last load; null when it has no owner thread, so a re-open reaches nobody. */
export type CommentLoad = { state: 'loading' | 'ready' | 'error'; error?: string; owner?: string | null; goesTo?: { id: string; name: string }[] };
export const commentLoads = writable<Record<string, CommentLoad>>({});
const placements = trackPlacements(docComments);
/** Retry a live row's placement: one new positions request for its document. */
export const retryPlacement = (commentId: string) => placements.retry(commentId);

/**
 * The one way a lane hold changes what is shown. The document's current rows, edited, go through the placement
 * owner exactly as a load's do, so a comment that becomes visible is placed and asked for like any other.
 * Comments still held stay out.
 */
export function reshow(artifactId: string, edit: (rows: DocComment[]) => DocComment[] = (rows) => rows): void {
	placements.show(artifactId, withhold(artifactId, edit(get(docComments)[artifactId] ?? []), shownStamp(artifactId)));
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

type ReloadListener = (artifactId: string, unseen: DocComment[]) => void;
const reloadListeners = new Set<ReloadListener>();
/** Told of every reload that succeeds, before it shows anything, with the rows it found that this canvas never showed. */
export function onCommentsReloaded(listener: ReloadListener): () => void {
	reloadListeners.add(listener);
	return () => reloadListeners.delete(listener);
}

async function fetchAndShow(artifactId: string): Promise<CommentFeedback> {
	// A refresh of comments already shown (a change frame from any canvas or
	// agent) stays 'ready': only a first load, or a retry after an error, reads
	// as loading and disables the comments control.
	if (get(commentLoads)[artifactId]?.state !== 'ready') {
		commentLoads.update((all) => ({ ...all, [artifactId]: { state: 'loading' } }));
	}
	try {
		const at = copyStamp();
		const { comments, feedback } = await fetchComments(artifactId);
		const rows = comments.map(fromServer);
		// What this canvas already showed stays through a failed load, so a retry after one still finds what is new.
		const shown = get(docComments)[artifactId];
		if (shown) {
			const known = new Set([...shown.map((row) => row.id), ...heldIds(artifactId)]);
			reloadListeners.forEach((listener) => listener(artifactId, rows.filter((row) => !known.has(row.id))));
		}
		placements.show(artifactId, withhold(artifactId, rows, at));
		commentLoads.update((all) => ({ ...all, [artifactId]: { state: 'ready', owner: feedback.owner_label, goesTo: feedback.goes_to ?? [] } }));
		return feedback;
	} catch (error) {
		commentLoads.update((all) => ({ ...all, [artifactId]: { state: 'error', error: error instanceof Error ? error.message : String(error) } }));
		throw error;
	}
}

function isAgent(author: CommentAuthor): boolean {
	return author !== 'you';
}

export function upsertTop(artifactId: string, row: ServerComment) {
	docComments.update((all) => {
		const list = all[artifactId] ? [...all[artifactId]] : [];
		if (list.some((c) => c.id === row.id)) return all;
		list.unshift(fromServer(row));
		return { ...all, [artifactId]: list };
	});
}

export async function addComment(artifactId: string, from: number, to: number, anchor: string, text: string, author: CommentAuthor = 'you', liveCapture = false, clientId?: string, taggedAgentIds: string[] = [], threadId: string | null = null, mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[] = [], again = false): Promise<string> {
	return postTop(artifactId, clientId ?? newClientId(), commentFields(anchor, from, to, text, author, liveCapture, taggedAgentIds, threadId, mentions), again);
}

/** What `addComment` posts; a live retry looks for its stored comment with the same fields (`findPosted`). */
export function commentFields(anchor: string, from: number, to: number, text: string, author: CommentAuthor = 'you', liveCapture = false, taggedAgentIds: string[] = [], threadId: string | null = null, mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[] = []): PostFields {
	return {
		body: text, anchor_text: anchor, from_off: from, to_off: to, ...(liveCapture ? { live_capture: true } : {}),
		author, author_kind: isAgent(author) ? 'agent' : 'user', tagged_agent_ids: taggedAgentIds, tagged_agent_mentions: mentions, thread_id: threadId
	};
}


/**
 * Post a reply under its draft's one id (`keptCommentDrafts.claim`), so a retry never stores it twice; `again` says an earlier
 * press used the id. When the reply stored under it was posted from other text or tags, that one shows in the thread and
 * this fails with `EarlierVersionPosted`, so the draft keeps the changes.
 */
export async function addReply(artifactId: string, commentId: string, text: string, author: CommentAuthor = 'you', taggedAgentIds: string[] = [], threadId: string | null = null, mentions: import('$lib/components/comments/mentionSelection').MentionOccurrence[] = [], clientId = newClientId(), again = false) {
	const show = (row: ServerComment) => docComments.update((all) => {
		const list = (all[artifactId] ?? []).map((c) => {
			if (c.id !== commentId) return c;
			if (c.replies.some((r) => r.id === row.id)) return c;
			return { ...c, replies: [...c.replies, { id: row.id, author, text: row.body ?? text, taggedAgents: row.tagged_agents ?? [], taggedPerson: row.tagged_person ?? null, mentions: row.tagged_agent_mentions ?? [], at: 'just now' }] };
		});
		return { ...all, [artifactId]: list };
	});
	let row: ServerComment;
	try {
		row = await postReconciled(artifactId, clientId, {
			body: text, parent_id: commentId, author, author_kind: isAgent(author) ? 'agent' : 'user', tagged_agent_ids: taggedAgentIds, tagged_agent_mentions: mentions, thread_id: threadId
		}, again);
	} catch (error) {
		if (error instanceof EarlierVersionPosted) show(error.row);
		throw error;
	}
	show(row);
}

/** Flips a flag at once and puts it back when the request fails. With `wait` the flag changes only once the request is answered: the caller shows the old state until then. Resolves true when the server took it. */
function patchFlag(artifactId: string, commentId: string, field: 'resolved' | 'acked', value: boolean, wait = false): Promise<boolean> {
	// Re-opening a thread takes back every acknowledgement on the server, so the card drops them too, not on the next load.
	const local: Partial<DocComment> = field === 'resolved' && !value ? { resolved: false, acked: false, acknowledgedBy: [] } : { [field]: value };
	let prev: Partial<DocComment> | undefined;
	const set = (next: Partial<DocComment>) => docComments.update((all) => {
		const list = (all[artifactId] ?? []).map((c) => {
			if (c.id !== commentId) return c;
			prev ??= Object.fromEntries(Object.keys(local).map((key) => [key, c[key as keyof DocComment]]));
			return { ...c, ...next };
		});
		return { ...all, [artifactId]: list };
	});
	if (!wait) set(local);
	return patchComment(commentId, { [field]: value }).then(() => { if (wait) set(local); return true; }, (e) => {
		console.error(`comment ${field} failed`, e);
		if (!wait && prev) set(prev);
		return false;
	});
}

export async function markThreadViewed(artifactId: string, rootId: string, seenReplyId: string): Promise<void> {
	const result = await markCommentThreadViewed(artifactId, rootId, seenReplyId);
	docComments.update((all) => ({
		...all,
		[artifactId]: (all[artifactId] ?? []).map((comment) => (
			comment.id === result.root_id ? { ...comment, newComment: false, newReplyIds: result.new_reply_ids } : comment
		)),
	}));
}

export function resolveComment(artifactId: string, commentId: string, resolved = true, wait = false): Promise<boolean> {
	return patchFlag(artifactId, commentId, 'resolved', resolved, wait);
}

export function ackComment(artifactId: string, commentId: string, acked = true): Promise<boolean> {
	return patchFlag(artifactId, commentId, 'acked', acked);
}

const dirty = new Map<string, Set<string>>();

/** `open`: the comments whose highlight had an open end before the edit (`cm/comment-marks.ts` `openCommentIds`). */
export function remapComments(artifactId: string, changes: ChangeDesc, open: ReadonlySet<string> = new Set()) {
	docComments.update((all) => {
		const list = (all[artifactId] ?? []).map((c) => {
			// A live row moves only by its own positions, and the server alone persists its offsets.
			if (c.drawn || c.placement || c.from == null || c.to == null) return c;
			// The highlight's rule and its open ends (`cm/comment-marks.ts`), so the card and its highlight never disagree.
			const { from, to } = mapCommentRange({ from: c.from, to: c.to, open: open.has(c.id) }, changes);
			const next = { ...c, from, to };
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
		&& fullText.slice(c.from, c.to) === anchoredQuote(c)
	) return { from: c.from, to: c.to };
	return null;
}
