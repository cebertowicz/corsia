import { get, writable } from 'svelte/store';
import type { ContextUsage } from './contextUsageTypes';
import type { Checkpoint } from './checkpoints';
import { findModelById, type Model } from './models';
import { fetchNextTurnPreview } from './nextTurnPreview';

/** Per-thread readings shared by the donut and sheet (#2269).
 * Measured usage is the last receipt; the preview estimates the next turn.
 * A newer turn refreshes the preview and a model change hides it.
 */
export const contextUsageByThread = writable<ReadonlyMap<string, ContextUsage>>(new Map());

export interface ContextPreviewEntry {
	/** Thread, model and measured reading the preview was read against. */
	owner: string;
	/** `owner` plus the latest checkpoint: a change re-reads the preview. */
	key: string;
	usage: ContextUsage | null;
	loading: boolean;
	error: string;
	/** The last preview may bridge a newer measured receipt. */
	retained?: boolean;
}

export const contextPreviewByThread = writable<ReadonlyMap<string, ContextPreviewEntry>>(new Map());

/** Threads whose first message page has been read since entry, reload or auth. */
export const measuredHydratedThreads = writable<ReadonlySet<string>>(new Set());

// Bumped by every invalidation, so a read that started before it cannot land.
const previewGenerations = new Map<string, number>();

type PreviewRequest = {
	threadId: string;
	owner: string;
	key: string;
	authToken: string;
	modelId: string | null;
	retryError?: boolean;
};

type ActivePreviewRead = { generation: number; pending: PreviewRequest | null; promise: Promise<boolean> };
type PreviewReadResult = { result: boolean; discardedByGeneration: boolean; entry: ContextPreviewEntry };

// One in-flight request owns each thread. New identities replace `pending`, so
// a burst of measured frames yields exactly one read after the current one.
const activePreviewReads = new Map<string, ActivePreviewRead>();

function setMapEntry<T>(
	store: { update(fn: (value: ReadonlyMap<string, T>) => ReadonlyMap<string, T>): void },
	threadId: string,
	value: T | null,
): void {
	store.update(entries => {
		if ((entries.get(threadId) ?? null) === value) return entries;
		const next = new Map(entries);
		if (value) next.set(threadId, value);
		else next.delete(threadId);
		return next;
	});
}

/** Record `threadId`'s measured usage; `null` forgets it. */
export function setContextUsage(threadId: string, usage: ContextUsage | null): void {
	setMapEntry(contextUsageByThread, threadId, usage);
}

/** Invalidate the preview; an unmeasured turn keeps the last receipt as stale. */
export function recordMeasuredUsage(threadId: string, usage: ContextUsage | null): void {
	invalidateContextPreview(threadId, true);
	const unmeasured = !usage || usage.token_usage_available === false;
	const kept = getContextUsage(threadId);
	if (unmeasured && kept) {
		if (!kept.stale) setContextUsage(threadId, { ...kept, stale: true });
		return;
	}
	if (!usage) return;
	setContextUsage(threadId, { ...usage, thread_id: threadId });
}

export function getContextUsage(threadId: string | null | undefined): ContextUsage | null {
	return threadId ? get(contextUsageByThread).get(threadId) ?? null : null;
}

function setHydrated(threadId: string, hydrated: boolean): void {
	measuredHydratedThreads.update(current => {
		if (current.has(threadId) === hydrated) return current;
		const next = new Set(current);
		if (hydrated) next.add(threadId);
		else next.delete(threadId);
		return next;
	});
}

/** Entering a thread: forget its reading until its first page is read again. */
export function beginThreadEntry(threadId: string): void {
	const active = activePreviewReads.get(threadId);
	if (active) active.pending = null;
	invalidateContextPreview(threadId);
	setMapEntry(contextPreviewByThread, threadId, null);
	setContextUsage(threadId, null);
	setHydrated(threadId, false);
}

type ReceiptRow = { thread_id?: string; metadata?: any };

/** Hydrate the newest stored receipt unless a live reading arrived first. */
export function hydrateMeasuredFromRows(threadId: string, rows: readonly ReceiptRow[]): void {
	if (!getContextUsage(threadId)) {
		for (let index = rows.length - 1; index >= 0; index--) {
			const row = rows[index];
			if (row.thread_id && row.thread_id !== threadId) continue;
			const receipt = row.metadata?.context_usage as ContextUsage | undefined;
			if (!receipt || typeof receipt !== 'object' || receipt.token_usage_available === false) continue;
			const model = typeof row.metadata?.model === 'string' ? row.metadata.model : null;
			setContextUsage(threadId, { ...receipt, thread_id: threadId, receipt_model: model });
			break;
		}
	}
	setHydrated(threadId, true);
}

function receiptModel(usage: ContextUsage): string | null {
	const facts = usage.context_snapshot?.harness_model_facts;
	const snapshotModel = typeof facts?.model === 'string' ? facts.model : null;
	return usage.receipt_model ?? snapshotModel;
}

/** Match exact model IDs or IDs resolved to the same catalog row. */
function sameModel(a: string, b: string, catalog: readonly Model[]): boolean {
	if (a === b) return true;
	const rowA = findModelById(catalog as Model[], a);
	return !!rowA && rowA === findModelById(catalog as Model[], b);
}

/** A measured reading belongs to this thread and selected model. */
export function measuredFor(
	usage: ContextUsage | null,
	threadId: string | null,
	modelId: string | null,
	catalog: readonly Model[] = [],
): ContextUsage | null {
	if (!usage || !threadId) return null;
	if (usage.thread_id && usage.thread_id !== threadId) return null;
	const model = receiptModel(usage);
	if (model && modelId && !sameModel(model, modelId, catalog)) return null;
	return usage;
}

/** Whether a reading carries a number the donut can draw. */
export function hasMeasuredNumbers(usage: ContextUsage | null): boolean {
	return !!usage && usage.token_usage_available !== false && typeof usage.total_used === 'number';
}

/** Who a preview belongs to, and when it must be read again. */
export function previewIdentity(
	threadId: string,
	modelId: string | null,
	measured: ContextUsage | null,
	latest: Checkpoint | null,
): { owner: string; key: string } {
	const owner = JSON.stringify([
		threadId,
		modelId,
		measured?.snapshot_id ?? null,
		measured?.token_source ?? null,
		measured?.token_usage_available ?? null,
		measured?.total_used ?? null,
		measured?.model_context_window ?? null,
	]);
	const checkpoint = [latest?.id ?? null, latest?.status ?? null, latest?.startedAt ?? null];
	return { owner, key: JSON.stringify([owner, checkpoint]) };
}

/** Whether two preview identities belong to the same thread and model. */
export function samePreviewModel(left: string, right: string): boolean {
	const [leftThread, leftModel] = JSON.parse(left);
	const [rightThread, rightModel] = JSON.parse(right);
	return leftThread === rightThread && leftModel === rightModel;
}

/** The reading both the donut and the sheet headline show: preview ?? measured. */
export function contextReading(
	preview: ContextPreviewEntry | undefined,
	owner: string,
	measured: ContextUsage | null,
): ContextUsage | null {
	return (preview?.owner === owner ? preview.usage : null) ?? measured;
}

/** Retire `threadId`'s preview. A read in flight settles before its replacement starts. */
export function invalidateContextPreview(threadId: string, retain = false): void {
	previewGenerations.set(threadId, (previewGenerations.get(threadId) ?? 0) + 1);
	const current = get(contextPreviewByThread).get(threadId);
	if (retain && current) setMapEntry(contextPreviewByThread, threadId, { ...current, retained: true });
	else if (!retain && !activePreviewReads.has(threadId)) setMapEntry(contextPreviewByThread, threadId, null);
}

async function readContextPreview(
	request: PreviewRequest,
	generation: number,
): Promise<PreviewReadResult> {
	const { threadId, owner, key } = request;
	const current = get(contextPreviewByThread).get(threadId);
	// A receipt changes the owner, but its model's previous preview can remain
	// visible until its replacement arrives.
	const shown = current && (current.owner === owner || (current.retained && samePreviewModel(current.owner, owner)))
		? current.usage : null;
	const entry = {
		owner, key, usage: shown, loading: true, error: '',
		retained: !!shown && (current?.owner !== owner || !!current?.retained),
	};
	setMapEntry(contextPreviewByThread, threadId, entry);
	const isCurrent = () =>
		(previewGenerations.get(threadId) ?? 0) === generation
		&& get(contextPreviewByThread).get(threadId) === entry;
	const discarded = () => ({
		result: false,
		discardedByGeneration: (previewGenerations.get(threadId) ?? 0) !== generation,
		entry,
	});
	try {
		const usage = await fetchNextTurnPreview(threadId, request.authToken, request.modelId);
		if (!isCurrent()) return discarded();
		const preview = usage ? { ...usage, thread_id: threadId } : null;
		setMapEntry(contextPreviewByThread, threadId, { owner, key, usage: preview, loading: false, error: '' });
		return { result: preview !== null, discardedByGeneration: false, entry };
	} catch (exc) {
		if (!isCurrent()) return discarded();
		const error = exc instanceof Error ? exc.message : 'Context preview unavailable';
		setMapEntry(contextPreviewByThread, threadId, {
			owner, key, usage: current?.owner === owner ? shown : null, loading: false, error,
		});
		return { result: false, discardedByGeneration: false, entry };
	}
}

function startContextPreviewRead(request: PreviewRequest): Promise<boolean> {
	const active: ActivePreviewRead = {
		generation: previewGenerations.get(request.threadId) ?? 0,
		pending: null,
		promise: Promise.resolve(false),
	};
	active.promise = (async () => {
		let next = request;
		while (true) {
			active.generation = previewGenerations.get(request.threadId) ?? 0;
			const attempt = await readContextPreview(next, active.generation);
			const pending = active.pending;
			active.pending = null;
			if (!pending) {
				const current = get(contextPreviewByThread).get(request.threadId);
				if (attempt.discardedByGeneration && current?.loading && current.key === attempt.entry.key) {
					setMapEntry(contextPreviewByThread, request.threadId, null);
				}
				if (activePreviewReads.get(request.threadId) === active) activePreviewReads.delete(request.threadId);
				return attempt.result;
			}
			next = pending;
		}
	})();
	activePreviewReads.set(request.threadId, active);
	return active.promise;
}

/**
 * Read `threadId`'s next-turn preview for `key` unless it is already held or
 * being read. A failed read is retried only when `retryError` asks, so an
 * error never turns into a request loop.
 */
export async function ensureContextPreview(request: PreviewRequest): Promise<boolean> {
	const current = get(contextPreviewByThread).get(request.threadId);
	const active = activePreviewReads.get(request.threadId);
	if (active) {
		const generationIsCurrent = active.generation === (previewGenerations.get(request.threadId) ?? 0);
		if (generationIsCurrent && !active.pending && current?.owner === request.owner && current.key === request.key) {
			return active.promise;
		}
		active.pending = request;
		return active.promise;
	}
	if (!current?.retained && current?.key === request.key && (current.loading || !current.error || !request.retryError)) {
		return current.usage !== null;
	}
	return startContextPreviewRead(request);
}

/** Forget every thread's reading, as when the connection identity resets. */
export function clearAllContextUsage(): void {
	contextUsageByThread.set(new Map());
	measuredHydratedThreads.set(new Set());
	for (const active of activePreviewReads.values()) active.pending = null;
	activePreviewReads.clear();
	for (const threadId of get(contextPreviewByThread).keys()) invalidateContextPreview(threadId);
}
