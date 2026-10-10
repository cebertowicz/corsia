import { get } from 'svelte/store';
import { on } from '$lib/ws/events';
import { fetchRevision } from '$lib/api/docs';
import { token } from '$lib/stores/auth';
import { threads } from '$lib/stores/threads';
import { subagentMeta } from '$lib/stores/subagents';
import { emitProductEvent } from '$lib/product_events';
import { uiScope } from '$lib/stores/uiScope';
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

interface PendingCheck {
	queued: boolean;
}

/** A tab identity and baseline are the guard against a late probe re-dirtying a save. */
interface ProbeTarget {
	path: string;
	artifactId: string | null;
	loadedRevision: string | null;
}

const pendingChecks = new Map<string, PendingCheck>();
let reportedProbeFailure = false;

function checkKey(threadId: string, instanceId: string): string {
	return `${threadId}\u0000${instanceId}`;
}

function sameProbeTarget(tab: ProbeTarget | undefined, target: ProbeTarget): boolean {
	return tab?.path === target.path
		&& tab.artifactId === target.artifactId
		&& tab.loadedRevision === target.loadedRevision;
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

function scheduleTabCheck(threadId: string, instanceId: string): void {
	const key = checkKey(threadId, instanceId);
	const pending = pendingChecks.get(key);
	if (pending) {
		pending.queued = true;
		return;
	}
	const next = { queued: false };
	pendingChecks.set(key, next);
	void runTabChecks(threadId, instanceId, key, next);
}

async function runTabChecks(
	threadId: string,
	instanceId: string,
	key: string,
	pending: PendingCheck,
): Promise<void> {
	try {
		do {
			pending.queued = false;
			const tab = get(threadWorkspaces)[threadId]?.tabs.find((entry) => entry.instanceId === instanceId);
			if (!tab || tab.kind !== 'document' || !get(token)) return;
			const target: ProbeTarget = {
				path: tab.path, artifactId: tab.artifactId, loadedRevision: tab.loadedRevision,
			};
			try {
				const result = await fetchRevision(target.path, get(uiScope)?.id ?? null, target.artifactId);
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
	for (const [threadId, ws] of Object.entries(get(threadWorkspaces))) {
		if (!isCurrentScopeThread(threadId)) continue;
		for (const tab of ws.tabs) {
			if (tab.kind === 'document') scheduleTabCheck(threadId, tab.instanceId);
		}
	}
}

/** Checks every tab of one thread when its workspace is selected. */
export function checkThreadRevisions(threadId: string | null): void {
	if (!threadId || !isCurrentScopeThread(threadId)) return;
	const ws = get(threadWorkspaces)[threadId];
	if (!ws) return;
	for (const tab of ws.tabs) {
		if (tab.kind === 'document') scheduleTabCheck(threadId, tab.instanceId);
	}
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
