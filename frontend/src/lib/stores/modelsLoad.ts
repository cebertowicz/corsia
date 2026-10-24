import { get } from 'svelte/store';
import { token } from './auth';
import { modelAvailability, type Model } from './models';
import { activeModelId, getModelDispatchId, models, threadCatalogs, workspaceModels } from './modelsState';
import { activeThreadId } from './threads';

// The thread the catalog was last read for: its harness rows read that
// thread's account, so another account's limit does not grey them out.
let loadedScope: string | null | undefined;
let loadSeq = 0;

function catalogScope(): string | null {
	return get(token) ? get(activeThreadId) : null;
}

async function readCatalog(scope: string | null, refreshUsage: boolean): Promise<Model[]> {
	const params = new URLSearchParams();
	if (refreshUsage) params.set('refresh_usage', 'true');
	if (scope) params.set('thread_id', scope);
	const query = params.toString();
	const path = query ? `/api/models?${query}` : '/api/models';
	// The network listener refuses any API read without the person's session, unscoped ones included.
	const session = get(token);
	const res = await (session ? fetch(path, { headers: { Authorization: `Bearer ${session}` } }) : fetch(path));
	if (res.ok === false) throw new Error(`catalog read returned ${res.status}`);
	const rows = (await res.json()).models;
	if (!Array.isArray(rows)) throw new Error('catalog read carried no rows');
	return rows;
}

// An unread account's status is unknown, never the last thread's: no row
// reads ready, so nothing sends on another account's verdict.
export function unknownStatus(rows: Model[]): Model[] {
	return rows.map((row) => ({ ...row, status: 'unavailable', status_reason: 'status_unknown', runs_on: undefined, runnable: undefined }));
}

async function loadScoped(scope: string | null, refreshUsage: boolean) {
	const seq = ++loadSeq;
	let rows: Model[];
	try {
		rows = await readCatalog(scope, refreshUsage);
	} catch (err) {
		if (!scope || seq !== loadSeq) return;
		console.warn('[models] thread catalog read failed; its statuses are unknown', err);
		loadedScope = scope;
		models.update(unknownStatus);
		return;
	}
	if (seq !== loadSeq) return;
	loadedScope = scope;
	models.set(rows);
	if (!scope) workspaceModels.set(rows);
	if (!get(activeThreadId) && !get(activeModelId)) {
		const runnable = (backend: string) => rows.find((m) => modelAvailability(m).status === 'ready' && m.backend === backend);
		const ready = runnable('local') || runnable('network') || runnable('api');
		if (ready) activeModelId.set(getModelDispatchId(ready));
	}
}

/** Reads the unscoped catalog and updates its shared snapshot. Callers that must surface a failure use this directly. */
export async function readWorkspaceModels(): Promise<Model[]> {
	const rows = await readCatalog(null, false);
	workspaceModels.set(rows);
	return rows;
}

// Chat keeps its best-effort background workspace refresh behavior.
async function loadWorkspace() {
	try {
		await readWorkspaceModels();
	} catch {
	}
}

/** Reads the open thread's catalog into `models` and the unscoped one into `workspaceModels`. */
export async function loadModels({ refreshUsage = false }: { refreshUsage?: boolean } = {}) {
	const scope = catalogScope();
	await Promise.all([loadScoped(scope, refreshUsage), scope ? loadWorkspace() : null]);
}

const threadLoadSeq = new Map<string, number>();

/**
 * Reads the catalog on another thread's account (a worker's) into `threadCatalogs`, so its composer
 * shows that thread's availability and accounts, not the open thread's.
 */
export async function loadThreadCatalog(threadId: string, refreshUsage = false): Promise<void> {
	if (!get(token)) return;
	const seq = (threadLoadSeq.get(threadId) ?? 0) + 1;
	threadLoadSeq.set(threadId, seq);
	let rows: Model[];
	try {
		rows = await readCatalog(threadId, refreshUsage);
	} catch (err) {
		if (threadLoadSeq.get(threadId) !== seq) return;
		console.warn('[models] thread catalog read failed; its statuses are unknown', err);
		rows = unknownStatus(get(workspaceModels));
	}
	if (threadLoadSeq.get(threadId) !== seq) return;
	threadCatalogs.update((all) => new Map(all).set(threadId, rows));
}

/** Reads the catalog a composer shows: the open thread's, or its own thread's when it is bound to another. */
export function loadComposerModels(threadId: string | null | undefined, opts: { refreshUsage?: boolean } = {}): Promise<void> {
	return threadId && threadId !== get(activeThreadId)
		? loadThreadCatalog(threadId, opts.refreshUsage)
		: loadModels(opts);
}

// Opening another thread re-reads the catalog on that thread's account.
activeThreadId.subscribe(() => {
	const scope = catalogScope();
	if (loadedScope !== undefined && scope !== loadedScope) void loadScoped(scope, false);
});
