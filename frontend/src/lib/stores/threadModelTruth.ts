import { writable } from 'svelte/store';

// The model each thread is held on, for every thread: one the thread list holds and one it does not
// (a worker). Kept apart from `models.ts` so the worker registry can seed it without an import cycle.

// `updatedAt` is null for a model a read reported without a server time (a worker's `subagent_meta`):
// any timed read replaces it.
const threadModelTruth = new Map<string, {
	model: string | null;
	updatedAt: string | null;
}>();

/** Bumped on every write to a thread's accepted model, so a derived reader of the map re-reads it. */
export const threadModelVersion = writable(0);

export function heldThreadModel(threadId: string) {
	return threadModelTruth.get(threadId);
}

export function holdThreadModel(threadId: string, model: string | null, updatedAt: string | null): void {
	threadModelTruth.set(threadId, { model, updatedAt });
	threadModelVersion.update((n) => n + 1);
}

export function clearHeldThreadModels(): void {
	threadModelTruth.clear();
	threadModelVersion.update((n) => n + 1);
}

function normalizedServerTime(value: string): string {
	return value.endsWith('Z') ? value.slice(0, -1) : value;
}

export function compareServerTimes(left: string, right: string): number {
	const normalizedLeft = normalizedServerTime(left);
	const normalizedRight = normalizedServerTime(right);
	if (normalizedLeft === normalizedRight) return 0;
	const leftMs = Date.parse(`${normalizedLeft}Z`);
	const rightMs = Date.parse(`${normalizedRight}Z`);
	if (!Number.isNaN(leftMs) && !Number.isNaN(rightMs) && leftMs !== rightMs) {
		return leftMs < rightMs ? -1 : 1;
	}
	return normalizedLeft < normalizedRight ? -1 : 1;
}

/** Whether a read stamped `updatedAt` outranks the model held; a model held without a time always yields. */
export function outranksHeld(
	updatedAt: string,
	current: { model: string | null; updatedAt: string | null },
	model: string | null,
): boolean {
	if (current.updatedAt === null) return true;
	const order = compareServerTimes(updatedAt, current.updatedAt);
	return order > 0 || (order === 0 && model === current.model);
}

export function newestDisplayTime(current: string | undefined, incoming: string): string {
	if (!current) return incoming;
	return compareServerTimes(incoming, current) >= 0 ? incoming : current;
}

/**
 * Record the model a read reports for a thread the list does not hold (a worker's `subagent_meta`
 * or status read), which carries no server time. It is the server's current row, so it replaces an
 * earlier read of the same kind, and it yields to any timed model.
 */
export function seedThreadModelTruth(threadId: string, model: string): void {
	if (!threadId || !model) return;
	const current = threadModelTruth.get(threadId);
	if (current && current.updatedAt !== null) return;
	if (current?.model !== model) holdThreadModel(threadId, model, null);
}

/** The accepted model of a thread, including one the thread list does not hold. */
export function threadModelFor(threadId: string): string | null {
	return threadModelTruth.get(threadId)?.model ?? null;
}
