import { get } from 'svelte/store';
import { docOpenFailure } from '$lib/docOpenRefusal';
import { token } from '$lib/stores/auth';
import type { PaneData } from './artifactPane';
import type { ArtifactRow, DocContent, DocRevision } from './docTypes';
import type { DocumentFreshness } from '$lib/docFreshness';
import type { UndeliveredBatch } from './docFeedback';

export type { ArtifactRow, DocContent, DocRevision } from './docTypes';

function tok(): string {
	return encodeURIComponent(get(token) ?? '');
}

export async function ok(res: Response) {
	if (!res.ok) {
		const detail = await res.text().catch(() => '');
		let parsed: unknown;
		try { parsed = JSON.parse(detail)?.detail; } catch { /* Non-JSON failures keep the HTTP status below. */ }
		const refusal = docOpenFailure(parsed);
		if (refusal) throw refusal;
		throw new Error(`${res.status} ${detail.slice(0, 200)}`);
	}
	return res.json();
}

export class PostTimeoutError extends Error {
	constructor(ms: number) {
		super(`request timed out after ${ms}ms (may have committed server-side)`);
		this.name = 'PostTimeoutError';
	}
}

export const COMMENT_POST_TIMEOUT_MS = 12_000;

export async function fetchWithTimeout(input: string, init: RequestInit, ms: number): Promise<Response> {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), ms);
	try {
		return await fetch(input, { ...init, signal: ctrl.signal });
	} catch (e) {
		if (e instanceof DOMException && e.name === 'AbortError') throw new PostTimeoutError(ms);
		throw e;
	} finally {
		clearTimeout(timer);
	}
}

export interface TreeNode {
	rel: string;
	name: string;
	size: number;
	mtime: number;
	tmp: boolean;
	artifact?: {
		id: string;
		description: string | null;
		owner_thread_id: string;
		owner_thread: string | null;
		updated_at: string;
	};
	owner_name?: string;
	freshness?: Pick<DocumentFreshness, 'state'>;
}
export interface TreeRoot {
	id: string;
	label: string;
	kind: 'workspace' | 'repo';
	path: string;
	nodes: TreeNode[];
	capped: boolean;
}

export async function fetchTree(projectId?: string | null): Promise<TreeRoot[]> {
	const q = projectId ? `&project_id=${encodeURIComponent(projectId)}` : '';
	const data = await ok(await fetch(`/api/files/tree?token=${tok()}${q}`));
	return data.roots;
}

/**
 * Read-only revision probe for an open tab.
 *
 * Never registers the read to a thread and never emits a change notification,
 * so it is safe to call on a change nudge or when the window regains focus.
 */
export async function fetchRevision(
	path: string,
	projectId?: string | null,
	artifactId?: string | null,
): Promise<DocRevision> {
	const q = `&path=${encodeURIComponent(path)}`
		+ (projectId ? `&project_id=${encodeURIComponent(projectId)}` : '')
		+ (artifactId ? `&artifact_id=${encodeURIComponent(artifactId)}` : '');
	return ok(await fetch(`/api/files/revision?token=${tok()}${q}`));
}

export async function fetchContent(path: string, opts: { projectId?: string | null; threadId?: string | null; liveOpen?: boolean } = {}): Promise<DocContent> {
	const q =
		`&path=${encodeURIComponent(path)}` +
		(opts.projectId ? `&project_id=${encodeURIComponent(opts.projectId)}` : '') +
		(opts.threadId ? `&thread_id=${encodeURIComponent(opts.threadId)}` : '') +
		(opts.liveOpen ? '&live_open=true' : '');
	return ok(await fetch(`/api/files/content?token=${tok()}${q}`));
}

export interface DocWriteResult {
	saved: boolean;
	size: number;
	mtime: number;
	revision: string;
	artifact?: ArtifactRow | null;
	/** Set when the file saved but its context-change notice could not be
	 * recorded; the save is still complete and the text is shown once. */
	context_notice_error?: string | null;
}

export async function writeContent(path: string, text: string, opts: { projectId?: string | null; threadId?: string | null; create?: boolean; description?: string | null } = {}): Promise<DocWriteResult> {
	return ok(await fetch(`/api/files/content?token=${tok()}`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ path, text, description: opts.description ?? null, project_id: opts.projectId ?? null, thread_id: opts.threadId ?? null, create: opts.create ?? false })
	}));
}

export async function registerArtifact(path: string, opts: { projectId?: string | null; threadId?: string | null } = {}): Promise<ArtifactRow> {
	const data = await ok(await fetch(`/api/files/register?token=${tok()}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ path, project_id: opts.projectId ?? null, thread_id: opts.threadId ?? null })
	}));
	return data.artifact;
}

export async function patchDescription(artifactId: string, description: string): Promise<ArtifactRow> {
	const data = await ok(await fetch(`/api/artifacts/${artifactId}?token=${tok()}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ description })
	}));
	return data.artifact;
}

export async function markArtifactVerified(artifactId: string): Promise<ArtifactRow> {
	const response = await fetch(`/api/artifacts/${artifactId}/verify?token=${tok()}`, { method: 'POST' });
	if (!response.ok) {
		const body = await response.json().catch(() => null);
		throw new Error(typeof body?.detail === 'string' ? body.detail : 'Could not mark this document verified.');
	}
	return (await response.json()).artifact;
}

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
}

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

export async function fetchComments(artifactId: string): Promise<CommentList> {
	const data = await ok(await fetch(`/api/artifacts/${artifactId}/comments?token=${tok()}`));
	return data;
}

export async function postComment(
	artifactId: string,
	c: { body: string; parent_id?: string; anchor_text?: string; from_off?: number; to_off?: number; review?: boolean; author?: string; author_kind?: 'user' | 'agent'; client_id?: string; live_capture?: boolean }
): Promise<ServerComment> {
	const data = await ok(await fetchWithTimeout(
		`/api/artifacts/${artifactId}/comments?token=${tok()}`,
		{ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(c) },
		COMMENT_POST_TIMEOUT_MS
	));
	return data.comment;
}

export async function patchComment(commentId: string, fields: Partial<{ resolved: boolean; acked: boolean; body: string; from_off: number; to_off: number; anchor_text: string }>): Promise<ServerComment> {
	const data = await ok(await fetch(`/api/comments/${commentId}?token=${tok()}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(fields)
	}));
	return data.comment;
}

export async function markCommentThreadViewed(artifactId: string, rootId: string, seenReplyId: string): Promise<{ root_id: string; reply_waiting: boolean }> {
	return ok(await fetch(`/api/artifacts/${artifactId}/comments/${rootId}/viewed`, {
		method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${get(token) ?? ''}` },
		body: JSON.stringify({ seen_reply_id: seenReplyId })
	}));
}

export async function persistPositions(artifactId: string, updates: { id: string; from_off: number; to_off: number }[]) {
	if (!updates.length) return;
	await ok(await fetch(`/api/artifacts/${artifactId}/comments/positions?token=${tok()}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ updates })
	}));
}

export async function linkRepo(projectId: string, path: string) {
	return ok(await fetch(`/api/projects/${projectId}/repos?token=${tok()}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ mode: 'link', path })
	}));
}

/** Hide (quiet) or restore (visible) an artifact registration in one thread's pane. */
export async function setArtifactDisplay(
	artifactId: string,
	threadId: string,
	hidden: boolean
): Promise<ArtifactRow> {
	const data = await ok(await fetch(`/api/artifacts/${artifactId}/display?token=${tok()}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ thread_id: threadId, hidden })
	}));
	return data.artifact;
}

/**
 * Pin or unpin an artifact in one thread's pane.
 *
 * A pinned artifact is kept when the plan task it was produced under settles,
 * and leads its list -- the top-level pane, or its own worktree group if the
 * file lives in one (pinning does not lift a file out of its group). This is
 * the override for the path-based default: a throwaway note can live in a
 * durable directory, and a durable file that lives in a worktree can be
 * pinned so it survives that worktree's own lifecycle.
 */
export async function setArtifactPinned(
	artifactId: string,
	threadId: string,
	pinned: boolean
): Promise<ArtifactRow> {
	const data = await ok(await fetch(`/api/artifacts/${artifactId}/pinned?token=${tok()}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ thread_id: threadId, pinned })
	}));
	return data.artifact;
}

/** Read one thread's artifact pane. Read-only: the server repairs after responding. */
export async function fetchArtifactPane(threadId: string): Promise<PaneData> {
	return ok(await fetch(`/api/threads/${threadId}/artifacts?token=${tok()}`));
}

export interface ReconcileReceipt {
	id: string;
	artifact_id: string;
	action: string;
	status: 'applied' | 'skipped' | 'failed';
	reason: string | null;
}

export interface ReconcileApplyResult {
	ok: boolean;
	mode: 'apply';
	counts: { applied: number; skipped: number; failed: number };
	receipts: ReconcileReceipt[];
}

/** Idempotently retry known artifact repairs for one thread (explicit apply, not a GET side effect). */
export async function reconcileArtifactsApply(threadId: string): Promise<ReconcileApplyResult> {
	return ok(await fetch(`/api/threads/${threadId}/artifacts/reconcile?token=${tok()}`, {
		method: 'POST'
	}));
}
