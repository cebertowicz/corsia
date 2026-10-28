import { get, writable } from 'svelte/store';
import type { ContextUsage } from './contextUsageTypes';
export { previewIdentity, contextReading } from './contextPreviewIdentity';
import { findModelById, type Model } from './models';
import { fetchNextTurnPreview } from './nextTurnPreview';

/**
 * The one owner of a thread's context reading: the donut and the context
 * sheet headline both render `contextReading`, so they cannot disagree.
 *
 * Keyed by the thread whose numbers they are (#2269), so every visible
 * composer, the active thread's and a subagent conversation's in the artifact
 * leaf, reads its own thread's entry.
 *
 * - `measured` is the last receipt: a live frame, or on entry the newest
 *   receipt stored on the thread's first message page.
 * - `preview` is the next-turn estimate, kept per thread with the identity it
 *   was read for. A turn retires it; an open sheet carries the previous
 *   reading while its replacement loads.
 */
export const contextUsageByThread = writable<ReadonlyMap<string, ContextUsage>>(new Map());

export interface ContextPreviewEntry {
	/** Thread, model and measured reading the preview was read against. */
	owner: string | null;
	/** `owner` plus the latest checkpoint: a change re-reads the preview. */
	key: string | null;
	usage: ContextUsage | null;
	loading: boolean;
	error: string;
	previous: ContextUsage | null;
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
	carry?: boolean;
};

type ActivePreviewRead = { generation: number; pending: PreviewRequest | null; promise: Promise<boolean> };
type PreviewReadResult = { result: boolean; discardedByGeneration: boolean; entry: ContextPreviewEntry };

// One in-flight request owns each thread. New identities replace `pending`, so
// a burst of measured frames yields exactly one read after the current one.
const activePreviewReads = new Map<string, ActivePreviewRead>();
const openContextSheets = new Set<string>();

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

/**
 * Record a turn's measured usage. Any turn retires the preview, which it has
 * made older; a turn that could not measure (no usage, or usage marked
 * unavailable) keeps the reading already shown rather than blanking it, but
 * marks it stale so it stops standing in for a receipt and the next preview
 * read replaces it.
 */
export function recordMeasuredUsage(threadId: string, usage: ContextUsage | null): void {
	invalidateContextPreview(threadId);
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
	setContextUsage(threadId, null);
	setHydrated(threadId, false);
}

type ReceiptRow = { thread_id?: string; metadata?: any };

/**
 * Fill `threadId`'s measured reading from the newest stored receipt in a
 * fetched first page. A live reading that arrived first is newer and is kept.
 */
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

/**
 * Two ids name one model when they are the same string, or when the catalog
 * resolves both to one row by the canonical rule (`modelMatchesId`). An id the
 * catalog has no row for matches only by exact string.
 */
function sameModel(a: string, b: string, catalog: readonly Model[]): boolean {
	if (a === b) return true;
	const rowA = findModelById(catalog as Model[], a);
	return !!rowA && rowA === findModelById(catalog as Model[], b);
}

/**
 * The measured reading this composer may show: its own thread's, and not
 * taken on a model other than the one selected.
 */
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

/** Whether `threadId`'s context sheet is open: only an open sheet carries a retired reading. */
export function setContextSheetOpen(threadId: string, open: boolean): void {
	if (open) { openContextSheets.add(threadId); return; }
	openContextSheets.delete(threadId);
	contextPreviewByThread.update(entries => {
		const current = entries.get(threadId);
		if (!current?.previous) return entries;
		// In place, so a read in flight still owns its entry and can land.
		current.previous = null;
		return new Map(entries);
	});
}
/** Retire `threadId`'s preview. A read in flight settles before its replacement starts. */
export function invalidateContextPreview(threadId: string): void {
	previewGenerations.set(threadId, (previewGenerations.get(threadId) ?? 0) + 1);
	const current = get(contextPreviewByThread).get(threadId);
	if (current) setMapEntry(contextPreviewByThread, threadId, {
		owner: null, key: null, usage: null, previous: openContextSheets.has(threadId) ? current.usage ?? current.previous : null, loading: false, error: '',
	});
}

function loadingPreview(request: PreviewRequest, current: ContextPreviewEntry | undefined): ContextPreviewEntry {
	return {
		owner: request.owner, key: request.key,
		usage: current?.owner === request.owner ? current.usage : null,
		previous: request.carry ? current?.usage ?? current?.previous ?? null : null,
		loading: true, error: '',
	};
}

async function readContextPreview(
	request: PreviewRequest,
	generation: number,
): Promise<PreviewReadResult> {
	const { threadId, owner, key } = request;
	const current = get(contextPreviewByThread).get(threadId);
	const entry = loadingPreview(request, current);
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
		setMapEntry(contextPreviewByThread, threadId, { owner, key, usage: preview, previous: null, loading: false, error: '' });
		return { result: preview !== null, discardedByGeneration: false, entry };
	} catch (exc) {
		if (!isCurrent()) return discarded();
		const error = exc instanceof Error ? exc.message : 'Context preview unavailable';
		setMapEntry(contextPreviewByThread, threadId, {
			owner, key, usage: entry.usage, previous: null, loading: false, error,
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
				if (attempt.discardedByGeneration && get(contextPreviewByThread).get(request.threadId) === attempt.entry) {
					invalidateContextPreview(request.threadId);
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
		if (active.pending?.key === request.key && current?.key === request.key) return active.promise;
		active.pending = request;
		setMapEntry(contextPreviewByThread, request.threadId, loadingPreview(request, current));
		return active.promise;
	}
	if (current?.key === request.key && (current.loading || !current.error || !request.retryError)) {
		return current.usage !== null;
	}
	return startContextPreviewRead(request);
}

/** Forget every thread's reading, as when the connection identity resets. */
export function clearAllContextUsage(): void {
	contextUsageByThread.set(new Map());
	openContextSheets.clear();
	measuredHydratedThreads.set(new Set());
	for (const active of activePreviewReads.values()) active.pending = null;
	activePreviewReads.clear();
	for (const threadId of get(contextPreviewByThread).keys()) invalidateContextPreview(threadId);
	contextPreviewByThread.set(new Map());
}
