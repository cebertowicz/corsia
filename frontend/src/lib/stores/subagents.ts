/**
 * Delegated-subagent metadata.
 *
 * The canvas owns the transcript (canonical messages + canonical WS events);
 * this store holds only per-subagent METADATA: identity, parent linkage, live
 * status (drives the inline chip and the subagent header), and init info from
 * the provider.
 *
 * Live WS frames are not the only writer. They cannot be: a reload empties an
 * in-memory map, and a chip that then falls back to a word like "open" makes an
 * affirmative claim about a worker it knows nothing about — the same shape of
 * bug as a model chip that forgets a thread was routing to Codex after a cold
 * open. So the default is `unknown`, and `hydrateSubagentThread` re-reads the
 * server's runtime truth instead of guessing.
 */

import { get, writable } from 'svelte/store';
import type { Thread } from './threads';

/**
 * `unknown` is the absence of information, never a claim about the worker.
 * `idle` is a server-confirmed "not running" — it does NOT say how the worker
 * ended, because runtime truth cannot tell us that.
 */
export type SubStatus = 'running' | 'ready' | 'closed' | 'error' | 'idle' | 'unknown';

/** Live frames own these; hydration may never overwrite one. */
const TERMINAL_STATUSES: ReadonlySet<SubStatus> = new Set<SubStatus>(['closed', 'error']);

/**
 * The thread-row fields, beyond the model, that the composer's effort and switch
 * controls read. The thread list never holds a worker, so a worker carries them
 * here and the composer reads them the way it reads any thread's. The worker's
 * selected model is `SubagentRef.model`, its one copy in this store.
 */
export type SubagentThreadFields = Partial<Pick<
	Thread,
	| 'reasoning_effort' | 'project_id'
	| 'pending_model' | 'switch_status' | 'switch_error' | 'switch_operation_id'
>>;

export interface SubagentRef {
	childThreadId: string;
	parentThreadId: string;
	name: string;
	provider: string;
	status: SubStatus;
	/** The worker's selected model: the provider's report at start, then every accepted switch. */
	model?: string;
	cwd?: string;
	thread?: SubagentThreadFields;
}

export const subagentMeta = writable<Map<string, SubagentRef>>(new Map());

export const openSubagentRequest = writable<string | null>(null);

export function registerSubagent(
	parentId: string,
	childId: string,
	name: string,
	provider = 'cc',
	status: SubStatus = 'running'
): void {
	subagentMeta.update((m) => {
		const n = new Map(m);
		const prev = n.get(childId);
		n.set(childId, {
			childThreadId: childId,
			parentThreadId: parentId,
			name,
			provider,
			status,
			model: prev?.model,
			cwd: prev?.cwd,
			thread: prev?.thread,
		});
		return n;
	});
}

/** Fold effort, project and switch fields into a worker's row; a thread that is no worker is ignored. */
export function patchSubagentThread(childId: string, fields: SubagentThreadFields): void {
	subagentMeta.update((m) => {
		const prev = m.get(childId);
		if (!prev) return m;
		const n = new Map(m);
		n.set(childId, { ...prev, thread: { ...prev.thread, ...fields } });
		return n;
	});
}

export function setSubagentStatus(childId: string, status: SubStatus): void {
	subagentMeta.update((m) => {
		const prev = m.get(childId);
		if (!prev) return m;
		const n = new Map(m);
		n.set(childId, { ...prev, status });
		return n;
	});
}

export function setSubagentInit(childId: string, init: { model?: string; cwd?: string }): void {
	subagentMeta.update((m) => {
		const prev = m.get(childId);
		if (!prev) return m;
		const n = new Map(m);
		n.set(childId, { ...prev, ...init });
		return n;
	});
}

export function setSubagentParent(childId: string, parentThreadId: string): void {
	subagentMeta.update((m) => {
		const prev = m.get(childId);
		if (!prev || prev.parentThreadId === parentThreadId) return m;
		const n = new Map(m);
		n.set(childId, { ...prev, parentThreadId });
		return n;
	});
}

/** Drop a thread the server does not consider a subagent. */
export function forgetSubagent(childId: string): void {
	subagentMeta.update((m) => {
		if (!m.has(childId)) return m;
		const n = new Map(m);
		n.delete(childId);
		return n;
	});
}

/** Runtime truth → the one status word the chip is allowed to show. */
export function statusFromRuntimeTruth(truth: Record<string, unknown>): SubStatus {
	if (truth.busy === true || truth.turn_active === true) return 'running';
	if (truth.cc_live === true || truth.codex_live === true) return 'ready';
	return 'idle';
}

const hydrationVersions = new Map<string, number>();
const hydrated = new Set<string>();

/**
 * Re-read one thread's runtime truth from the server and fold it into the map.
 *
 * Called for every spawn chip on screen and for the thread the user is looking
 * at, so a reload does not erase what the system knows — including the fact
 * that the open thread IS a subagent, which is what routes a send to the
 * child's stdin channel and keeps the Send button live.
 *
 * Degradation is declared, not silent: a failed read leaves the chip `unknown`
 * and logs at error. Never invents a status.
 */
export async function hydrateSubagentThread(
	threadId: string,
	token: string,
	parentThreadId = '',
): Promise<SubStatus | null> {
	if (!threadId || !token) return null;
	const version = (hydrationVersions.get(threadId) ?? 0) + 1;
	hydrationVersions.set(threadId, version);
	const before = get(subagentMeta).get(threadId);
	const statusBefore = before?.status ?? null;
	try {
		const res = await fetch(
			`/api/delegation/threads/${encodeURIComponent(threadId)}/status`
			+ `?token=${encodeURIComponent(token)}&messages=0`,
		);
		if (!res.ok) throw new Error(`subagent status refresh failed (${res.status})`);
		const truth = await res.json();
		if (hydrationVersions.get(threadId) !== version) return null;
		if (truth?.thread_kind !== 'subagent') {
			// Membership in this map is what routes a send to a child's stdin
			// channel, so an entry the server disowns is not cosmetic.
			forgetSubagent(threadId);
			return null;
		}

		// The server names a worker's parent; it is what opens the worker beside it.
		const serverParent = typeof truth?.parent_thread_id === 'string' ? truth.parent_thread_id : '';
		if (serverParent) setSubagentParent(threadId, serverParent);
		const current = get(subagentMeta).get(threadId);
		// The composer reads these on a worker in any status, so they fold in before
		// either early return. A field a live frame or the user changed while this
		// request was in flight is newer than the response and stays.
		const foldThreadFields = () => {
			const now = get(subagentMeta).get(threadId);
			if (!now) return;
			if (typeof truth?.model === 'string' && truth.model && now.model === before?.model) {
				setSubagentInit(threadId, { model: truth.model });
			}
			const fields: SubagentThreadFields = {};
			if (now.thread?.reasoning_effort === before?.thread?.reasoning_effort) {
				fields.reasoning_effort = typeof truth?.reasoning_effort === 'string' ? truth.reasoning_effort : null;
			}
			if (now.thread?.project_id === before?.thread?.project_id) {
				fields.project_id = typeof truth?.project_id === 'string' ? truth.project_id : null;
			}
			patchSubagentThread(threadId, fields);
		};
		// A live frame that landed while this request was in flight is newer than
		// the response, and a terminal outcome is the last word either way.
		if (current && current.status !== statusBefore) { foldThreadFields(); return current.status; }
		if (current && TERMINAL_STATUSES.has(current.status)) { foldThreadFields(); return current.status; }

		const status = statusFromRuntimeTruth(truth ?? {});
		registerSubagent(
			// Linkage stays empty rather than wrong when nobody knows it.
			serverParent || current?.parentThreadId || parentThreadId,
			threadId,
			(typeof truth?.title === 'string' && truth.title) || current?.name || 'subagent',
			(typeof truth?.driver === 'string' && truth.driver) || current?.provider || 'cc',
			status,
		);
		foldThreadFields();
		return status;
	} catch (error) {
		// Declared, not silent: the chip stays `unknown` and a later mount retries.
		console.error(`[subagents] runtime truth unavailable for ${threadId}:`, error);
		hydrated.delete(threadId);
		return null;
	}
}

/** Hydrate once per thread per page load; chips re-render constantly. */
export function hydrateSubagentThreadOnce(
	threadId: string,
	token: string,
	parentThreadId = '',
): void {
	if (!threadId || !token || hydrated.has(threadId)) return;
	hydrated.add(threadId);
	void hydrateSubagentThread(threadId, token, parentThreadId);
}

export function _resetSubagentHydrationForTests(): void {
	hydrationVersions.clear();
	hydrated.clear();
}

export interface SubagentOpenTarget {
	parentThreadId: string;
	childThreadId: string;
	title: string;
}

/**
 * Resolves an `openSubagentRequest` to its leaf-tab target.
 *
 * `null` when the parent link is unknown: the header's whole job is the way
 * back, so a chip whose worker has no known parent stays inert rather than
 * opening a leaf with no way back to it.
 */
export function resolveSubagentOpenTarget(
	childThreadId: string,
	meta: ReadonlyMap<string, SubagentRef>,
): SubagentOpenTarget | null {
	const ref = meta.get(childThreadId);
	if (!ref?.parentThreadId) return null;
	return { parentThreadId: ref.parentThreadId, childThreadId, title: ref.name };
}
