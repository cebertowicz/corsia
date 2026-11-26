import { get } from 'svelte/store';
import { on } from '$lib/ws/events';
import { fetchRevision, type DocRevision } from '$lib/api/docs';
import { token } from '$lib/stores/auth';
import { threads } from '$lib/stores/threads';
import { subagentMeta } from '$lib/stores/subagents';
import { emitProductEvent } from '$lib/product_events';
import { uiScope } from '$lib/stores/uiScope';
import { activeThreadId } from '$lib/stores/activeThreadId';
import { threadWorkspaces, applyTabRevisionCheck } from '$lib/stores/artifactWorkspace';

/**
 * Keeps every open tab's "latest known revision" honest.
 *
 * The `artifacts_changed` websocket event carries no revision and also fires on
 * reads and registration, so it is treated purely as a hint to *go and check*,
 * never as evidence that content changed. The authority is the read-only
 * revision probe. Focus is the second trigger, which covers changes that
 * happened while the app was closed or the socket was down.
 */

/**
 * Background probes may hold at most this many of the browser's connections at
 * once. Safari over HTTP/1.1 opens six per origin, and a request the person
 * just made (a comment post) waits in the browser while all six are held. A
 * foreground sweep sends one probe per open document tab, so without a limit
 * it can hold all six. Probes the person asked for (a link press, a draft
 * load, an image view) call fetchRevision directly, outside this limit, and
 * never queue behind a sweep.
 */
const MAX_BACKGROUND_PROBES = 2;

interface SlotWaiter {
	visible: () => boolean;
	grant: () => void;
}

let probesInFlight = 0;
const slotWaiters: SlotWaiter[] = [];

/** Takes a slot now (null) or returns the wait for one. */
function takeProbeSlot(visible: () => boolean): Promise<void> | null {
	if (probesInFlight < MAX_BACKGROUND_PROBES) {
		probesInFlight += 1;
		return null;
	}
	return new Promise((grant) => slotWaiters.push({ visible, grant }));
}

/** Hands the slot to the visible tab's waiting probe first, otherwise the oldest. */
function releaseProbeSlot(): void {
	const visibleIndex = slotWaiters.findIndex((waiter) => waiter.visible());
	const [next] = slotWaiters.splice(visibleIndex >= 0 ? visibleIndex : 0, 1);
	if (next) next.grant();
	else probesInFlight -= 1;
}

/** A tab identity and baseline are the guard against a late probe re-dirtying a save. */
interface ProbeTarget {
	path: string;
	artifactId: string | null;
	loadedRevision: string | null;
}

/** One revision request; `started` turns true once it is on the wire. */
interface Probe {
	started: boolean;
	result: Promise<DocRevision>;
}

/** One foreground sweep asks once per file and gives the answer to every tab holding it. */
type SweepProbes = Map<string, Probe>;

interface PendingCheck {
	queued: boolean;
	probe: Probe | null;
}

const pendingChecks = new Map<string, PendingCheck>();
let reportedProbeFailure = false;

function checkKey(threadId: string, instanceId: string): string {
	return `${threadId}\u0000${instanceId}`;
}

function fileKey(target: ProbeTarget): string {
	return `${target.path}\u0000${target.artifactId ?? ''}`;
}

function sameProbeTarget(tab: ProbeTarget | undefined, target: ProbeTarget): boolean {
	return tab?.path === target.path
		&& tab.artifactId === target.artifactId
		&& tab.loadedRevision === target.loadedRevision;
}

/** The tab the person is looking at: the active tab of the active thread. */
function visibleTabId(): { threadId: string; instanceId: string } | null {
	const threadId = get(activeThreadId);
	const instanceId = threadId ? get(threadWorkspaces)[threadId]?.activeTabId : null;
	return threadId && instanceId ? { threadId, instanceId } : null;
}

function isVisibleFile(target: ProbeTarget): boolean {
	const visible = visibleTabId();
	if (!visible) return false;
	const tab = get(threadWorkspaces)[visible.threadId]?.tabs.find((entry) => entry.instanceId === visible.instanceId);
	return tab?.path === target.path && tab.artifactId === target.artifactId;
}

function startProbe(target: ProbeTarget, sweep: SweepProbes | null): Probe {
	const shared = sweep?.get(fileKey(target));
	if (shared) return shared;
	const probe = { started: false } as Probe;
	const send = () => {
		probe.started = true;
		const result = fetchRevision(target.path, get(uiScope)?.id ?? null, target.artifactId);
		// Released on a side branch, so the tabs waiting on `result` hear it no later.
		result.then(releaseProbeSlot, releaseProbeSlot);
		return result;
	};
	const wait = takeProbeSlot(() => isVisibleFile(target));
	probe.result = wait ? wait.then(send) : send();
	sweep?.set(fileKey(target), probe);
	return probe;
}

/** Persisted tabs are global, but probes must stay within the loaded UI scope. */
function isCurrentScopeThread(threadId: string): boolean {
	const projectId = get(uiScope)?.id ?? null;
	// `||`, not `??`: a ref whose parent is not known yet carries an empty
	// string, and scoping to "" matches no thread at all.
	const parentId = get(subagentMeta).get(threadId)?.parentThreadId || threadId;
	return get(threads).some((thread) => (
		thread.id === parentId && (thread.project_id ?? null) === projectId
	));
}

function scheduleTabCheck(threadId: string, instanceId: string, sweep: SweepProbes | null = null): void {
	const key = checkKey(threadId, instanceId);
	const pending = pendingChecks.get(key);
	if (pending) {
		// A probe still waiting for a slot will read the newest revision when it
		// goes out; one already on the wire may predate this change.
		if (!pending.probe || pending.probe.started) pending.queued = true;
		return;
	}
	const next: PendingCheck = { queued: false, probe: null };
	pendingChecks.set(key, next);
	void runTabChecks(threadId, instanceId, key, next, sweep);
}

async function runTabChecks(
	threadId: string,
	instanceId: string,
	key: string,
	pending: PendingCheck,
	sweep: SweepProbes | null,
): Promise<void> {
	try {
		do {
			pending.queued = false;
			const tab = get(threadWorkspaces)[threadId]?.tabs.find((entry) => entry.instanceId === instanceId);
			// A link tab still resolving names no file yet, so there is nothing to probe.
			if (!tab || tab.kind !== 'document' || tab.link || !get(token)) return;
			const target: ProbeTarget = {
				path: tab.path, artifactId: tab.artifactId, loadedRevision: tab.loadedRevision,
			};
			// Only the first look shares the sweep's answer; a trailing look is newer.
			pending.probe = startProbe(target, sweep);
			sweep = null;
			try {
				const result = await pending.probe.result;
				const current = get(threadWorkspaces)[threadId]?.tabs.find((entry) => entry.instanceId === instanceId);
				// A local save/reload/identity resolution while this request was in flight
				// is newer than the response. Never let that response re-dirty the tab.
				if (sameProbeTarget(current, target)) {
					applyTabRevisionCheck(threadId, instanceId, {
						exists: result.exists,
						path: result.path,
						revision: result.revision,
						writable: result.writable,
						artifactId: result.artifact_id,
					});
				}
			} catch {
				// A failed probe is not evidence that the document is gone — leave the
				// tab exactly as it was rather than inventing an unavailable state.
				if (!reportedProbeFailure) {
					reportedProbeFailure = true;
					emitProductEvent('artifact', 'revision_probe_failed');
				}
			}
		} while (pending.queued);
	} finally {
		if (pendingChecks.get(key) === pending) pendingChecks.delete(key);
	}
}

/** Document tabs to check, the visible one first so its answer comes back first. */
function tabsToCheck(threadIds: string[]): Array<{ threadId: string; instanceId: string }> {
	const all = get(threadWorkspaces);
	const found: Array<{ threadId: string; instanceId: string }> = [];
	for (const threadId of threadIds) {
		if (!isCurrentScopeThread(threadId)) continue;
		for (const tab of all[threadId]?.tabs ?? []) {
			if (tab.kind === 'document') found.push({ threadId, instanceId: tab.instanceId });
		}
	}
	const visible = visibleTabId();
	const first = found.findIndex((entry) => (
		entry.threadId === visible?.threadId && entry.instanceId === visible.instanceId
	));
	if (first > 0) found.unshift(...found.splice(first, 1));
	return found;
}

/** Checks every tab in every thread that holds this artifact, in any thread. */
function checkArtifact(artifactId: string | null | undefined): void {
	if (!artifactId) return;
	const all = get(threadWorkspaces);
	for (const [threadId, ws] of Object.entries(all)) {
		if (!isCurrentScopeThread(threadId)) continue;
		for (const tab of ws.tabs) {
			if (tab.kind === 'document' && tab.artifactId === artifactId) scheduleTabCheck(threadId, tab.instanceId);
		}
	}
}

/** Checks every currently open tab, including inactive thread workspaces. */
export function checkAllRevisions(): void {
	const sweep: SweepProbes = new Map();
	for (const { threadId, instanceId } of tabsToCheck(Object.keys(get(threadWorkspaces)))) {
		scheduleTabCheck(threadId, instanceId, sweep);
	}
}

/** Checks every tab of one thread when its workspace is selected. */
export function checkThreadRevisions(threadId: string | null): void {
	if (!threadId) return;
	for (const entry of tabsToCheck([threadId])) scheduleTabCheck(entry.threadId, entry.instanceId);
}

export function startArtifactRevisionWatch(): () => void {
	const offChanged = on('artifacts_changed', (payload: { artifact_id?: string }) => {
		checkArtifact(payload?.artifact_id);
	});

	// Browsers normally emit focus and visibilitychange for one foregrounding.
	// Keep one bounded, watcher-local burst so the second event cannot schedule a
	// full second sweep after the first requests settle.
	let lastForegroundProbeAt = Number.NEGATIVE_INFINITY;
	const onForeground = () => {
		const now = Date.now();
		if (now - lastForegroundProbeAt < 250) return;
		lastForegroundProbeAt = now;
		checkAllRevisions();
	};
	const onVisible = () => {
		if (typeof document !== 'undefined' && document.visibilityState === 'visible') onForeground();
	};

	if (typeof window !== 'undefined') {
		window.addEventListener('focus', onForeground);
		document.addEventListener('visibilitychange', onVisible);
	}

	return () => {
		offChanged?.();
		if (typeof window !== 'undefined') {
			window.removeEventListener('focus', onForeground);
			document.removeEventListener('visibilitychange', onVisible);
		}
	};
}
