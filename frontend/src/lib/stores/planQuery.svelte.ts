/**
 * Declaration/subscription primitive. Components declare which plan keys
 * they render; this layer owns observation refcounts, cold load, ambient
 * recovery triggers, and retirement. Components never issue, schedule, or
 * cancel a request themselves — every trigger below funnels into
 * `refresh()`/`refreshObserved()` in `plans.svelte.ts`.
 *
 * Not a second state owner: this module holds only observation bookkeeping
 * (refcount, host links) and delegates every read/write to `plans.svelte.ts`.
 */
import { get } from 'svelte/store';
import { untrack } from 'svelte';
import { token } from './auth';
import { connectionState } from './connection';
import { on } from '$lib/ws';
import type { ActivityIndexEnvelope, OperatorEnvelope } from '$lib/api/plans';
import {
	EMPTY_ENTRY, activityEntryFor, operatorEntryFor, cancelAll, enforceCacheCap, peekEntry, refresh, retire,
	type PlanEntry,
} from './plans.svelte';
import { cancelDeferred } from './planRefreshSpacing';
import { keyMatchesChange, parseKeyString, planKeyString, type ActivityKey, type OperatorKey, type OperatorKeyString, type PlanKey, type PlansChangedMessage } from './planKeys';

const CACHE_CAP = 12;
/** Same floor and ceiling as the chat list's own overdue timer (`threadPlanStates`). */
const MIN_OVERDUE_RELOAD_MS = 5_000;
const MAX_TIMEOUT_MS = 2_147_483_647;

/** The earliest instant a wait in the payload's plans may become overdue, or null. */
function nextOverdueMs(payload: unknown): number | null {
	const plans = (payload as OperatorEnvelope | null)?.plans;
	if (!Array.isArray(plans)) return null;
	const instants = plans
		.map((entry) => entry.display?.not_moving?.next_overdue_at)
		.map((at) => (at ? Date.parse(at) : Number.NaN))
		.filter(Number.isFinite);
	return instants.length ? Math.min(...instants) : null;
}

interface Observation {
	key: PlanKey;
	count: number;
	/** Key strings of hosts whose payload embeds this key's progress (parent → child). */
	hosts: Set<string>;
}

const observers = new Map<string, Observation>();
let ambientStop: (() => void) | null = null;

function refreshObserved() {
	for (const rec of [...observers.values()]) void refresh(rec.key);
}

function applyNudge(message: PlansChangedMessage) {
	const hostStrs = new Set<string>();
	for (const rec of [...observers.values()]) {
		const entry = peekEntry(planKeyString(rec.key));
		if (!message.plans.some((change) => keyMatchesChange(rec.key, entry.ownerThreadId, change))) continue;
		void refresh(rec.key);
		for (const hostStr of rec.hosts) hostStrs.add(hostStr);
	}
	// A child mutation nudges only the child thread; the parent payload embeds
	// that child's progress, so an observed child refreshes its host too.
	for (const hostStr of hostStrs) {
		const host = observers.get(hostStr);
		if (host) void refresh(host.key);
	}
}

function startAmbient() {
	if (ambientStop) return;
	const offNudge = on('plans_changed', (message: PlansChangedMessage) => applyNudge(message));
	let wasConnected = get(connectionState) === 'connected';
	const offConnection = connectionState.subscribe((state) => {
		const connected = state === 'connected';
		if (connected && !wasConnected) refreshObserved();
		wasConnected = connected;
	});
	let lastToken = get(token);
	const offToken = token.subscribe((value) => {
		if (value === lastToken) return;
		lastToken = value;
		cancelAll();
		refreshObserved();
	});
	ambientStop = () => {
		offNudge();
		offConnection();
		offToken();
	};
}

function stopAmbient() {
	ambientStop?.();
	ambientStop = null;
}

function observe(key: PlanKey, hostStr: string | null) {
	const keyStr = planKeyString(key);
	const rec = observers.get(keyStr);
	if (rec) {
		rec.count += 1;
		if (hostStr) rec.hosts.add(hostStr);
		return;
	}
	observers.set(keyStr, { key, count: 1, hosts: new Set(hostStr ? [hostStr] : []) });
	startAmbient();
	void refresh(key);
}

function release(key: PlanKey, retireEmptyPending = false) {
	const keyStr = planKeyString(key);
	const rec = observers.get(keyStr);
	if (!rec) return;
	rec.count -= 1;
	if (rec.count > 0) return;
	observers.delete(keyStr);
	// Nothing renders the key now: a refresh still waiting out its gap has no reader.
	cancelDeferred(keyStr);
	// A cold detail collapsed while loading owns no visible result. Retire its
	// request identity so the eventual response cannot populate the cache.
	if (retireEmptyPending && peekEntry(keyStr).payload === null) retire(keyStr);
	// A collapsed child detail has no further purpose; scope/thread payloads stay
	// cached (bounded) so navigation return is instant and revalidates by ETag.
	else if (key.kind === 'child') retire(keyStr);
	else enforceCacheCap(new Set(observers.keys()), CACHE_CAP);
	if (observers.size === 0) stopAmbient();
}

export interface QueryHandle<P> {
	readonly status: PlanEntry['status'];
	readonly refreshing: boolean;
	readonly payload: P | null;
	readonly errorMessage: string | null;
	readonly lastFreshAt: number | null;
	retry(): void;
}

/**
 * Declares exactly one key for as long as the calling component renders it.
 *
 * Identity is the canonical key *string*: a caller may rebuild its `PlanKey`
 * object on every recomputation without churning observation or refetching.
 */
function queryHandle<K extends PlanKey, P>(
	getKey: () => K | null,
	readEntry: (key: K) => PlanEntry<P>,
	options: { retireEmptyPendingOnRelease?: boolean } = {},
): QueryHandle<P> {
	const keyStr = $derived.by(() => {
		const key = getKey();
		return key ? planKeyString(key) : null;
	});
	$effect(() => {
		const current = keyStr;
		if (!current) return;
		const key = parseKeyString(current);
		if (!key) return;
		// Untracked: declaring a key must never make this effect depend on the
		// payload the declaration loads.
		untrack(() => observe(key, null));
		return () => untrack(() => release(key, options.retireEmptyPendingOnRelease ?? false));
	});
	const entry = $derived.by(() => {
		const key = getKey();
		return key ? readEntry(key) : EMPTY_ENTRY;
	});
	// Becoming overdue is a time-driven change no write announces: re-read once
	// when the earliest wait crosses its limit. A one-shot timer, re-armed by
	// the payload it loads, never an interval.
	$effect(() => {
		const next = nextOverdueMs(entry.payload);
		if (next === null) return;
		const delay = Math.min(MAX_TIMEOUT_MS, Math.max(MIN_OVERDUE_RELOAD_MS, next - Date.now()));
		const timer = setTimeout(() => {
			const key = untrack(getKey);
			if (key) void refresh(key);
		}, delay);
		return () => clearTimeout(timer);
	});
	return {
		get status() { return entry.status; },
		get refreshing() { return entry.refreshing; },
		get payload() { return entry.payload; },
		get errorMessage() { return entry.errorMessage; },
		get lastFreshAt() { return entry.lastFreshAt; },
		retry() {
			const key = getKey();
			if (key) void refresh(key);
		},
	};
}

export type PlanQueryHandle = QueryHandle<OperatorEnvelope>;
export type ActivityQueryHandle = QueryHandle<ActivityIndexEnvelope>;

export function planQuery(
	getKey: () => OperatorKey | null,
	options: { retireEmptyPendingOnRelease?: boolean } = {},
): PlanQueryHandle {
	return queryHandle(getKey, operatorEntryFor, options);
}

export function activityQuery(
	getKey: () => ActivityKey | null,
	options: { retireEmptyPendingOnRelease?: boolean } = {},
): ActivityQueryHandle {
	return queryHandle(getKey, activityEntryFor, options);
}

export interface PlanKeySetHandle {
	state(key: OperatorKey): PlanEntry<OperatorEnvelope>;
	retry(key: OperatorKey): void;
}

/** Declares a changing set of keys, diffing so unchanged keys never churn. */
export function planKeySet(getKeys: () => OperatorKey[], getHost?: () => PlanKey | null): PlanKeySetHandle {
	let current = new Map<OperatorKeyString, OperatorKey>();
	$effect(() => {
		const desired = new Map(getKeys().map((key) => [planKeyString(key), key]));
		const host = getHost?.() ?? null;
		const hostStr = host ? planKeyString(host) : null;
		untrack(() => {
			for (const [keyStr, key] of current) if (!desired.has(keyStr)) release(key);
			for (const [keyStr, key] of desired) if (!current.has(keyStr)) observe(key, hostStr);
		});
		current = desired;
	});
	$effect(() => () => untrack(() => {
		for (const [, key] of current) release(key);
		current = new Map();
	}));
	return {
		state: (key: OperatorKey) => operatorEntryFor(key),
		retry: (key: OperatorKey) => { void refresh(key); },
	};
}

export function observerCountForTests(keyStr: string): number {
	return observers.get(keyStr)?.count ?? 0;
}

export function ambientActiveForTests(): boolean {
	return ambientStop !== null;
}

export function resetObserversForTests(): void {
	stopAmbient();
	observers.clear();
}
