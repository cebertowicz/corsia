/**
 * The canonical plan-key owner. Owns, for every plan key: entry state
 * (status/payload/etag/error/freshness), request issue + coalescing,
 * monotonic request identity, response admission, retirement, and
 * cancellation. Nothing else may write plan state — see `entryFor`/
 * `peekEntry` below for the two sanctioned read paths.
 *
 * Identity rule: request ids come from one module-global monotonic counter
 * that is never reset per key. A request owns the key only while the key's
 * live runtime still carries that exact id — `ownedRuntime` below. Every
 * continuation asks that one question, so a superseded task can neither
 * write plan state nor touch the request that replaced it.
 *
 * Concurrency rule: at most one *owning* request per key. A refresh requested
 * while one is in flight sets `pending` and is issued exactly once afterwards,
 * so same-key responses can never resolve out of order. A retired or
 * cancelled request may still be outstanding on the network, but it owns
 * nothing.
 * Spacing: `planRefreshSpacing.ts` decides when an Activity key may start one.
 */
import { untrack } from 'svelte';
import { get } from 'svelte/store';
import { token } from './auth';
import * as api from '$lib/api/plans';
import type {
	ActivityIndexEnvelope,
	ActivityIndexReadOutcome,
	OperatorEnvelope,
	OperatorReadOutcome,
} from '$lib/api/plans';
import { deferRefresh, forgetSpacing, noteStarted, spacingWaitMs } from './planRefreshSpacing';
import { planClosedMessage } from './planClosedMessage';
import { errorText, safeErrorMessage, STALE_PAGE_MESSAGE } from '$lib/api/planRead';
import { isActivityKey, planKeyString, type ActivityKey, type ActivityKeyString, type OperatorKey, type OperatorKeyString, type PlanKey, type PlanKeyString } from './planKeys';

/**
 * Preserve HTTP meaning until the consuming surface can explain it. A stale
 * scope (404), an authenticated but forbidden scope (403) and sign-in absence
 * are distinct product states.
 */
export type PlanLoadStatus =
	| 'idle' | 'loading' | 'ready' | 'stale' | 'error'
	| 'unauthorized' | 'forbidden' | 'not-found';

export interface PlanEntry<P = OperatorEnvelope | ActivityIndexEnvelope> {
	status: PlanLoadStatus;
	/** A refresh is in flight over an already-rendered payload — no banner, no skeleton. */
	refreshing: boolean;
	payload: P | null;
	etag: string | null;
	errorMessage: string | null;
	reloadNeeded: boolean; // a reply this page can't read: a retry fails the same way, a page reload can fix it
	lastFreshAt: number | null;
	ownerThreadId: string | null;
}

export const EMPTY_ENTRY: PlanEntry<never> = {
	status: 'idle', refreshing: false, payload: null, etag: null,
	errorMessage: null, reloadNeeded: false, lastFreshAt: null, ownerThreadId: null,
};

let entries = $state.raw<Record<string, PlanEntry>>({});

/** The one tracked read. Use only from a template or `$derived`. */
export function entryFor(keyStr: OperatorKeyString): PlanEntry<OperatorEnvelope>;
export function entryFor(keyStr: ActivityKeyString): PlanEntry<ActivityIndexEnvelope>;
export function entryFor(keyStr: PlanKeyString): PlanEntry;
export function entryFor(keyStr: PlanKeyString): PlanEntry {
	const entry = entries[keyStr] ?? EMPTY_ENTRY;
	const activity = keyStr === 'activity:workspace' || keyStr.startsWith('activity-project:');
	if (entry.payload && entry.payload.contract_version !== (activity ? 1 : 2)) {
		throw new Error('Plan cache representation mismatch');
	}
	return entry;
}

/** The untracked read. Every imperative path uses this, so declaring a key
 * from an `$effect` can never make that effect depend on the payload it
 * just loaded (which would re-run the effect and re-issue the request
 * forever). */
export function peekEntry(keyStr: string): PlanEntry {
	return untrack(() => entries)[keyStr] ?? EMPTY_ENTRY;
}

/** Typed reads check the private heterogeneous cache at its representation boundary. */
export function operatorEntryFor(key: OperatorKey): PlanEntry<OperatorEnvelope> {
	return entryFor(planKeyString(key));
}

export function activityEntryFor(key: ActivityKey): PlanEntry<ActivityIndexEnvelope> {
	return entryFor(planKeyString(key));
}

interface Runtime {
	requestId: number | null;
	inFlight: Promise<void> | null;
	pending: boolean;
	/** Insertion order for the unobserved-entry cache cap. */
	seq: number;
}

const runtimes = new Map<string, Runtime>();
let requestSeq = 0;
let runtimeSeq = 0;

function patch(keyStr: string, delta: Partial<PlanEntry>) {
	const snapshot = untrack(() => entries);
	entries = { ...snapshot, [keyStr]: { ...(snapshot[keyStr] ?? EMPTY_ENTRY), ...delta } };
}

function runtimeFor(keyStr: string): Runtime {
	let rt = runtimes.get(keyStr);
	if (!rt) {
		rt = { requestId: null, inFlight: null, pending: false, seq: ++runtimeSeq };
		runtimes.set(keyStr, rt);
	}
	return rt;
}

type PlanReadOutcome = OperatorReadOutcome | ActivityIndexReadOutcome;

function fetchForKey(key: PlanKey, bearer: string, etag: string | null): Promise<PlanReadOutcome> {
	if (key.kind === 'workspace') return api.readOperatorWorkspace(bearer, etag);
	if (key.kind === 'project') return api.readOperatorProject(bearer, key.id, etag);
	if (key.kind === 'thread') return api.readOperatorThread(bearer, key.id, etag);
	if (key.kind === 'child') return api.readOperatorChild(bearer, key.id, etag);
	if (key.kind === 'activity-workspace') return api.readActivityWorkspace(bearer, etag);
	return api.readActivityProject(bearer, key.id, etag);
}

function ownerThreadIdFor(
	key: PlanKey,
	payload: OperatorEnvelope | ActivityIndexEnvelope,
): string | null {
	if (key.kind === 'thread' || key.kind === 'child') return payload.target.id;
	return null;
}

/** Sign-in, which a retry cannot change; not-found and forbidden are worded per place in `planClosedMessage`. */
const SIGN_IN_MESSAGE = 'Sign in to see this plan.';

function applyOutcome(key: PlanKey, keyStr: string, outcome: PlanReadOutcome) {
	if (outcome.status === 'fresh') {
		const expectedVersion = isActivityKey(key) ? 1 : 2;
		if (outcome.payload.contract_version !== expectedVersion) {
			applyOutcome(key, keyStr, { status: 'error', message: STALE_PAGE_MESSAGE, reload: true });
			return;
		}
		patch(keyStr, {
			status: 'ready', refreshing: false, payload: outcome.payload, etag: outcome.etag,
			errorMessage: null, reloadNeeded: false, lastFreshAt: Date.now(),
			ownerThreadId: ownerThreadIdFor(key, outcome.payload),
		});
		return;
	}
	if (outcome.status === 'not-modified') {
		patch(keyStr, { status: 'ready', refreshing: false, errorMessage: null, reloadNeeded: false, lastFreshAt: Date.now() });
		return;
	}
	if (outcome.status !== 'error') {
		patch(keyStr, {
			status: outcome.status, refreshing: false, payload: null, etag: null,
			ownerThreadId: null, reloadNeeded: false, errorMessage: outcome.status === 'unauthorized'
				? SIGN_IN_MESSAGE : planClosedMessage(key, outcome.status),
		});
		return;
	}
	const current = peekEntry(keyStr);
	const failure = { refreshing: false, errorMessage: errorText(outcome), reloadNeeded: outcome.reload === true };
	patch(keyStr, current.payload
		? { status: 'stale', ...failure }
		: { status: 'error', payload: null, ...failure });
}

/**
 * The key's live runtime, but only while `myId` is still its current request.
 *
 * This is the one ownership rule, and every continuation asks it: a
 * superseded task may neither write plan state nor touch the runtime that
 * replaced it.
 */
function ownedRuntime(keyStr: string, myId: number): Runtime | null {
	const live = runtimes.get(keyStr);
	return live && live.requestId === myId ? live : null;
}

async function issue(key: PlanKey, keyStr: string, myId: number): Promise<void> {
	const bearer = get(token);
	const before = peekEntry(keyStr);
	if (!bearer) {
		patch(keyStr, before.payload
			? { status: 'stale', refreshing: false, errorMessage: SIGN_IN_MESSAGE, reloadNeeded: false }
			: { status: 'unauthorized', refreshing: false, payload: null, errorMessage: SIGN_IN_MESSAGE, reloadNeeded: false });
		return;
	}
	patch(keyStr, { status: before.payload ? before.status : 'loading', refreshing: true });
	const outcome = await fetchForKey(key, bearer, before.etag);
	if (!ownedRuntime(keyStr, myId)) return; // retired, cancelled, or superseded
	applyOutcome(key, keyStr, outcome);
}

/**
 * Records an unexpected failure in the request path as an ordinary failed
 * read, so no path can leave the key permanently `refreshing`.
 */
function admitReadFailure(key: PlanKey, keyStr: string, myId: number): void {
	if (!ownedRuntime(keyStr, myId)) return;
	applyOutcome(key, keyStr, { status: 'error', message: safeErrorMessage('load') });
}

/**
 * Hands back the in-flight slot this request holds, then issues the single
 * follow-up queued while it was running. A superseded request owns no slot,
 * so it releases nothing and starts nothing.
 */
function releaseSlot(key: PlanKey, keyStr: string, myId: number): void {
	const live = ownedRuntime(keyStr, myId);
	if (!live) return;
	live.inFlight = null;
	if (!live.pending) return;
	live.pending = false;
	// Scheduled, not awaited: the returned promise settles with *this* request
	// so a caller can never be chained forever by re-invalidation.
	void refresh(key);
}

/** The one write path. Coalesces: at most one in-flight and one queued per key. */
export function refresh(key: PlanKey): Promise<void> {
	const keyStr = planKeyString(key);
	const rt = runtimeFor(keyStr);
	if (rt.inFlight) {
		rt.pending = true;
		return rt.inFlight;
	}
	const waitMs = spacingWaitMs(key);
	if (waitMs > 0) return deferRefresh(key, waitMs, () => refresh(key));
	noteStarted(key);
	const myId = ++requestSeq;
	rt.requestId = myId;
	const running = (async () => {
		try {
			await issue(key, keyStr, myId);
		} catch {
			admitReadFailure(key, keyStr, myId);
		} finally {
			releaseSlot(key, keyStr, myId);
		}
	})();
	rt.inFlight = running;
	return running;
}

/** Retire a key: drops the entry and the runtime, so any outstanding response is discarded. */
export function retire(key: PlanKey | string): void {
	const keyStr = typeof key === 'string' ? key : planKeyString(key);
	forgetSpacing(keyStr);
	runtimes.delete(keyStr);
	const snapshot = untrack(() => entries);
	if (keyStr in snapshot) {
		const { [keyStr]: _dropped, ...rest } = snapshot;
		entries = rest;
	}
}

/** Auth epoch change: every outstanding response becomes inadmissible. */
export function cancelAll(): void {
	for (const rt of runtimes.values()) {
		rt.requestId = null;
		rt.inFlight = null;
		rt.pending = false;
	}
	forgetSpacing();
	// An auth epoch change retires the old payload too. Keeping its ETag would
	// let the new identity send a conditional read and retain the old body on
	// a 304; clearing all entry material makes the boundary explicit.
	const snapshot = untrack(() => entries);
	entries = Object.fromEntries(
		Object.keys(snapshot).map((keyStr) => [keyStr, { ...EMPTY_ENTRY }]),
	);
}

/** Bounds cache growth: keeps every observed key plus the newest `cap` unobserved ones. */
export function enforceCacheCap(observed: Set<string>, cap: number): void {
	const unobserved = [...runtimes.entries()]
		.filter(([keyStr]) => !observed.has(keyStr))
		.sort((a, b) => b[1].seq - a[1].seq);
	for (const [keyStr] of unobserved.slice(cap)) retire(keyStr);
}

export function resetPlanRuntimeForTests(): void {
	forgetSpacing();
	entries = {};
	runtimes.clear();
	requestSeq = 0;
	runtimeSeq = 0;
}

export function requestSeqForTests(): number {
	return requestSeq;
}
