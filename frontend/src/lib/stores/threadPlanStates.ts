/**
 * The chat list's plan state per thread (#2220). The server derives every
 * state by the plan header's rule; this store only holds the last reading.
 *
 * One batched read covers the listed threads. It is refreshed for the threads a
 * `plans_changed`, `thread_updates_changed` or `host_access_changed` nudge
 * names (the update dot's seen state, an access request), and in full on
 * reconnect and on sign-in. A failed read keeps the last reading, and the next
 * trigger retries.
 */
import { get, writable } from 'svelte/store';
import { readThreadPlanStates, type ThreadPlanStateEntry } from '$lib/api/plans';
import { on } from '$lib/ws';
import { token } from './auth';
import { connectionState } from './connection';
import type { PlansChangedMessage } from './planKeys';

export const threadPlanStates = writable<Record<string, ThreadPlanStateEntry>>({});

/** Whether a thread's plan waits on something outside: an empty thread then shows its waiting chip, not the greeting (EW8). */
export function waitsOutside(states: Record<string, ThreadPlanStateEntry>, threadId: string | null): boolean {
	const entry = threadId ? states[threadId] : undefined;
	return entry?.state === 'waiting' && !!entry.wait;
}

let listed = new Set<string>();
let seq = 0;
/** The newest read that has written each thread, so a slow older read never overwrites it. */
const appliedSeq = new Map<string, number>();
let stopAmbient: (() => void) | null = null;
/** Under the server's per-read cap, and short enough for any URL limit. */
const CHUNK = 100;

async function load(ids: string[]): Promise<void> {
	for (let start = 0; start < ids.length; start += CHUNK) {
		await loadChunk(ids.slice(start, start + CHUNK));
	}
}

async function loadChunk(ids: string[]): Promise<void> {
	const bearer = get(token);
	if (!bearer || ids.length === 0) return;
	const mine = ++seq;
	const states = await readThreadPlanStates(bearer, ids);
	if (states === null) return;
	threadPlanStates.update((current) => {
		const next = { ...current };
		for (const id of ids) {
			if ((appliedSeq.get(id) ?? 0) > mine) continue;
			appliedSeq.set(id, mine);
			if (states[id]) next[id] = states[id];
			else delete next[id];
		}
		return next;
	});
}

/** The listed threads a nudge's `thread_ids` names; anything else is ignored. */
function namedListed(value: unknown): string[] {
	const named = Array.isArray(value) ? value : [];
	return [...new Set(named.filter((id): id is string => typeof id === 'string' && listed.has(id)))];
}

function startAmbient(): () => void {
	const offNudge = on('plans_changed', (message: PlansChangedMessage) => {
		const named = message.plans
			.filter((change) => change.owner.kind === 'thread' && listed.has(change.owner.id))
			.map((change) => change.owner.id);
		void load([...new Set(named)]);
	});
	// An update, a visit or a Mark read/unread changes only the named threads' dots.
	const offUpdatesNudge = on('thread_updates_changed', (message: { thread_ids?: unknown }) => {
		void load(namedListed(message?.thread_ids));
	});
	// A lease is not a plan mutation, but its awaiting-user state shares the
	// same batched row projection. Re-read the threads its nudge names; a nudge
	// naming none (the server could not resolve them) re-reads every listed row.
	const offHostAccessNudge = on('host_access_changed', (message: { thread_ids?: unknown }) => {
		void load(message?.thread_ids === undefined ? [...listed] : namedListed(message.thread_ids));
	});
	let wasConnected = get(connectionState) === 'connected';
	const offConnection = connectionState.subscribe((state) => {
		const connected = state === 'connected';
		if (connected && !wasConnected) void load([...listed]);
		wasConnected = connected;
	});
	let lastToken = get(token);
	const offToken = token.subscribe((value) => {
		if (value === lastToken) return;
		lastToken = value;
		void load([...listed]);
	});
	return () => {
		offNudge();
		offUpdatesNudge();
		offHostAccessNudge();
		offConnection();
		offToken();
	};
}

/** Follow the chat list's threads; only threads new to the list are read at once. */
export function watchThreadPlanStates(threadIds: string[]): void {
	const added = threadIds.filter((id) => !listed.has(id));
	listed = new Set(threadIds);
	stopAmbient ??= startAmbient();
	void load(added);
}

/** Re-read named listed threads now, e.g. after this device marked one read or unread. */
export function reloadThreadPlanStates(threadIds: string[]): void {
	void load(threadIds.filter((id) => listed.has(id)));
}

export function stopThreadPlanStates(): void {
	stopAmbient?.();
	stopAmbient = null;
	listed = new Set();
}

export function resetThreadPlanStatesForTests(): void {
	stopThreadPlanStates();
	seq = 0;
	appliedSeq.clear();
	threadPlanStates.set({});
}
