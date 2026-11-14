/**
 * The chat list's plan state per thread (#2220). The server derives every
 * state by the plan header's rule; this store only holds the last reading.
 *
 * One batched read covers the listed threads and the thread whose chat is open on a
 * surface that hides the menu (the phone's plan chip). It is refreshed for the threads a
 * `plans_changed`, `thread_updates_changed`, `host_access_changed` or `held_reply_changed` nudge
 * names (the update dot's seen state, an access request), and in full on
 * reconnect and on sign-in. A failed read keeps the last reading, and the next
 * trigger retries. One read is out at a time: threads named while it is out join
 * one read that starts when it ends, so a burst of nudges costs at most two
 * reads and the last one reads the server's state after the burst.
 */
import { derived, get, writable } from 'svelte/store';
import { readThreadPlanStates, type ThreadPlanStateEntry } from '$lib/api/plans';
import { isYourMove } from '$lib/components/threadRowMark';
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

/**
 * When an idle plan's set-aside work comes back (EW7), or null. The server sends
 * `set_aside_until` for any set-aside task; only an idle plan reads set aside, so
 * one still working or owing you keeps its own state.
 */
export function planSetAsideUntil(entry: ThreadPlanStateEntry | undefined): string | null {
	return entry?.state === 'idle' && entry.set_aside_until ? entry.set_aside_until : null;
}

let listed = new Set<string>();
const listedIds = writable<ReadonlySet<string>>(listed);

/**
 * The thread whose chat is open on a surface that hides the menu (the phone's plan
 * chip). Its row is read and refreshed like a listed one, so that surface shows the
 * menu's own rule for a thread the menu may not list at all; it never joins
 * `listed`, so the menu's needs-you dot is unaffected.
 */
let openChats = new Set<string>();

/**
 * Whether any thread the menu lists shows its needs-you mark. On a phone the menu
 * is hidden, so the controls that open it carry the same dot.
 */
export const listedNeedsYou = derived(
	[threadPlanStates, listedIds],
	([states, ids]) => [...ids].some((id) => isYourMove(states[id])),
);
let stopAmbient: (() => void) | null = null;

/** Whether the store keeps this thread's reading current: the menu lists it, or its chat is open. */
function isTracked(id: string): boolean {
	return listed.has(id) || openChats.has(id);
}

function trackedIds(): string[] {
	return [...new Set([...listed, ...openChats])];
}

/** Nudges, reconnect and sign-in reads run while the menu lists threads or a chat is open. */
function syncAmbient(): void {
	if (listed.size > 0 || openChats.size > 0) stopAmbient ??= startAmbient();
	else {
		stopAmbient?.();
		stopAmbient = null;
	}
}

/** Under the server's per-read cap, and short enough for any URL limit. */
const CHUNK = 100;
/** Threads named since the read that is out started; the next read covers them all. */
let pending = new Set<string>();
let draining: Promise<void> | null = null;

function load(ids: string[]): Promise<void> {
	for (const id of ids) pending.add(id);
	if (!draining && pending.size > 0) draining = drain();
	return draining ?? Promise.resolve();
}

/**
 * Reads run one after another, so a newer reading always lands after an older one.
 * `draining` clears in the same step that finds `pending` empty, so a thread named
 * at any later moment starts a new read rather than joining one that has ended.
 */
async function drain(): Promise<void> {
	try {
		while (pending.size > 0) {
			const ids = [...pending];
			pending = new Set();
			for (let start = 0; start < ids.length; start += CHUNK) {
				await loadChunk(ids.slice(start, start + CHUNK));
			}
		}
	} finally {
		draining = null;
	}
}

async function loadChunk(ids: string[]): Promise<void> {
	const bearer = get(token);
	if (!bearer) return;
	const states = await readThreadPlanStates(ids);
	if (states === null) return;
	threadPlanStates.update((current) => {
		const next = { ...current };
		for (const id of ids) {
			if (states[id]) next[id] = states[id];
			else delete next[id];
		}
		return next;
	});
}

/** The tracked threads a nudge's `thread_ids` names; anything else is ignored. */
function namedTracked(value: unknown): string[] {
	const named = Array.isArray(value) ? value : [];
	return [...new Set(named.filter((id): id is string => typeof id === 'string' && isTracked(id)))];
}

function startAmbient(): () => void {
	const offNudge = on('plans_changed', (message: PlansChangedMessage) => {
		const named = message.plans
			.filter((change) => change.owner.kind === 'thread' && isTracked(change.owner.id))
			.map((change) => change.owner.id);
		void load([...new Set(named)]);
	});
	// An update, a visit or a Mark read/unread changes only the named threads' dots.
	const offUpdatesNudge = on('thread_updates_changed', (message: { thread_ids?: unknown }) => {
		void load(namedTracked(message?.thread_ids));
	});
	// A lease is not a plan mutation, but its awaiting-user state shares the
	// same batched row projection. Re-read the threads its nudge names; a nudge
	// naming none (the server could not resolve them) re-reads every listed row.
	const offHostAccessNudge = on('host_access_changed', (message: { thread_ids?: unknown }) => {
		void load(message?.thread_ids === undefined ? trackedIds() : namedTracked(message.thread_ids));
	});
	// A held reply puts a hold on its sender's row: re-read the threads the nudge names.
	const offHeldNudge = on('held_reply_changed', (message: { thread_ids?: unknown }) => {
		void load(namedTracked(message?.thread_ids));
	});
	let wasConnected = get(connectionState) === 'connected';
	const offConnection = connectionState.subscribe((state) => {
		const connected = state === 'connected';
		if (connected && !wasConnected) void load(trackedIds());
		wasConnected = connected;
	});
	let lastToken = get(token);
	const offToken = token.subscribe((value) => {
		if (value === lastToken) return;
		lastToken = value;
		void load(trackedIds());
	});
	return () => {
		offNudge();
		offUpdatesNudge();
		offHostAccessNudge();
		offHeldNudge();
		offConnection();
		offToken();
	};
}

/** Follow the chat list's threads; only threads new to the list are read at once. */
export function watchThreadPlanStates(threadIds: string[]): void {
	const added = threadIds.filter((id) => !listed.has(id));
	listed = new Set(threadIds);
	listedIds.set(listed);
	syncAmbient();
	void load(added);
}

/**
 * Follow the thread whose chat is open where the menu is hidden (the phone's plan
 * chip), so it shows the menu's own needs-you rule for a thread the menu may not
 * list. Only threads new to this set are read at once.
 */
export function watchOpenThreadPlanStates(threadIds: string[]): void {
	// A thread the menu already lists needs no extra read; its row is already kept current.
	const added = threadIds.filter((id) => !openChats.has(id) && !listed.has(id));
	openChats = new Set(threadIds);
	syncAmbient();
	void load(added);
}

/** Re-read named threads the store keeps current, e.g. after this device marked one read or unread. */
export function reloadThreadPlanStates(threadIds: string[]): void {
	void load(threadIds.filter(isTracked));
}

export function stopThreadPlanStates(): void {
	listed = new Set();
	listedIds.set(listed);
	syncAmbient();
}

export function resetThreadPlanStatesForTests(): void {
	stopThreadPlanStates();
	openChats = new Set();
	stopAmbient?.();
	stopAmbient = null;
	pending = new Set();
	draining = null;
	threadPlanStates.set({});
}
