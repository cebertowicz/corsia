import { writable } from 'svelte/store';
import type { Thread } from './threads';

// The model each thread is held on, for every thread: one the thread list holds and one it does not
// (a worker). Kept apart from `models.ts` so the worker registry can seed it without an import cycle.

/** A thread's model-switch state, as a listed row carries it. */
export type HeldSwitch = Partial<Pick<
	Thread,
	'pending_model' | 'switch_status' | 'switch_error' | 'switch_operation_id'
>>;

// `updatedAt` is null for a model a read reported without a server time (a worker's `subagent_meta`):
// any timed read replaces it. `switch` is held only for a thread the list omits (the scope's conductor
// or CORSIA agent), written by a read of its target; `switchAt` orders that thread's later switch writes.
const threadModelTruth = new Map<string, {
	model: string | null;
	updatedAt: string | null;
	switch?: HeldSwitch;
	switchAt?: number;
}>();

// A pending switch does not move a thread's `updated_at` (only the commit does), so a server time
// cannot order a read against the 202 or frame that changed it. Each read start and each write takes
// the next number; a thread's read may replace its switch only if nothing wrote to it since the read began.
let switchTick = 0;

/** Bumped on every write to a thread's accepted model, so a derived reader of the map re-reads it. */
export const threadModelVersion = writable(0);

export function heldThreadModel(threadId: string) {
	return threadModelTruth.get(threadId);
}

export function holdThreadModel(threadId: string, model: string | null, updatedAt: string | null): void {
	threadModelTruth.set(threadId, { ...threadModelTruth.get(threadId), model, updatedAt });
	threadModelVersion.update((n) => n + 1);
}

/** The switch state held for a thread the list omits, if a read of its target has set one. */
export function heldThreadSwitch(threadId: string): HeldSwitch | undefined {
	return threadModelTruth.get(threadId)?.switch;
}

/** Mark the start of a read of a thread's target, to hand back to `applySwitchRead`. */
export function beginSwitchRead(): number {
	return ++switchTick;
}

/** Replace a thread's switch state with a read begun at `startedAt`; a read older than the thread's last write or read is stale. */
export function applySwitchRead(threadId: string, startedAt: number, fields: HeldSwitch): void {
	const current = threadModelTruth.get(threadId) ?? { model: null, updatedAt: null };
	if (startedAt < (current.switchAt ?? 0)) return;
	threadModelTruth.set(threadId, { ...current, switch: fields, switchAt: startedAt });
	threadModelVersion.update((n) => n + 1);
}

/** Fold fields from the 202 or a frame into a switch state a read has set; any other thread is ignored. */
export function patchHeldSwitch(threadId: string, fields: HeldSwitch): void {
	const current = threadModelTruth.get(threadId);
	if (!current?.switch || Object.keys(fields).length === 0) return;
	threadModelTruth.set(threadId, { ...current, switch: { ...current.switch, ...fields }, switchAt: ++switchTick });
	threadModelVersion.update((n) => n + 1);
}

export function clearHeldThreadModels(): void {
	threadModelTruth.clear();
	switchTick = 0;
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
