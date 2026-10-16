import { get, writable } from 'svelte/store';
import { acceptThreadModelTruth } from '$lib/stores/models';
import { activeThreadId } from '$lib/stores/threads';
import type { UiScope } from '$lib/stores/uiScope';

export interface CoordinationTarget {
	thread_id: string;
	title?: string | null;
	model?: string | null;
	updated_at?: string | null;
	reasoning_effort?: string | null;
	project_id?: string | null;
	bootloader_files?: string[] | string | null;
	driver?: string | null;
	[key: string]: unknown;
}

/** Result of one coordination-target lookup.
 *
 * `absent` is a confirmed answer: the requested target truly does not exist for
 * this caller. Transport failures, non-OK responses, and a missing token are
 * `unknown`, so callers can retry instead of rendering a false absence from a
 * failed lookup. */
export type CoordinationLookup =
	| { kind: 'target'; target: CoordinationTarget; firstRunPending: boolean }
	| { kind: 'absent' }
	| { kind: 'unknown' };

/** The CORSIA agent's thread id once a lookup confirms it; its sources
 * panel shows only there (#3287). */
export const corsiaAgentId = writable<string | null>(null);

/** The CORSIA agent thread to land on at sign-in, or null to keep Activity.
 *
 * Only a pending first run in the workspace scope lands there, once per page
 * load, and never over a thread the user already opened (#3287 N2). */
export function firstRunLanding(
	lookup: CoordinationLookup,
	{ scope, activeThreadId, landed }: { scope: UiScope | null; activeThreadId: string | null; landed: boolean },
): string | null {
	if (landed || scope || activeThreadId) return null;
	return lookup.kind === 'target' && lookup.firstRunPending ? lookup.target.thread_id : null;
}

/** A first run still pending with no model chosen: the person's setup stopped before the AI step. */
export function setupIncomplete(lookup: CoordinationLookup): boolean {
	return lookup.kind === 'target' && lookup.firstRunPending && !lookup.target.model;
}

let firstRunLanded = false;

/** Open the CORSIA agent once per page load when `firstRunLanding` says so. */
export function landOnFirstRun(
	lookup: CoordinationLookup,
	scope: UiScope | null,
	open: (threadId: string) => unknown,
): void {
	const landing = firstRunLanding(lookup, {
		scope, activeThreadId: get(activeThreadId), landed: firstRunLanded,
	});
	if (!landing) return;
	firstRunLanded = true;
	void open(landing);
}

export function _resetFirstRunLandingForTests(): void {
	firstRunLanded = false;
}

export function conductorScopeParam(scope: UiScope | null): string {
	return scope?.id ?? 'workspace';
}

async function fetchTarget(token: string | null, url: string): Promise<CoordinationLookup> {
	if (!token) return { kind: 'unknown' };
	try {
		const res = await fetch(url);
		if (!res.ok) return { kind: 'unknown' };
		const data = await res.json();
		const threadId = data?.target?.thread_id ?? data?.thread_id ?? null;
		if (!threadId) return { kind: 'absent' };
		const target: CoordinationTarget = { ...(data?.target ?? {}), thread_id: threadId };
		// The thread list omits this target, so this read is its only row: it
		// enters the same server-time ordering every listed thread's model does.
		if (target.model && target.updated_at) {
			acceptThreadModelTruth(threadId, target.model, target.updated_at);
		}
		return { kind: 'target', target, firstRunPending: data?.first_run_pending === true };
	} catch {
		return { kind: 'unknown' };
	}
}

/** Read a project's conductor target and accept its model as that thread's truth. */
export function fetchConductor(token: string | null, scope: UiScope | null): Promise<CoordinationLookup> {
	if (!scope) return Promise.resolve({ kind: 'unknown' });
	return fetchTarget(
		token,
		`/api/conductor?token=${encodeURIComponent(token ?? '')}&scope=${encodeURIComponent(scope.id)}`,
	);
}

/** Read the workspace CORSIA agent and accept its model as that thread's truth. */
export async function fetchCorsiaAgent(token: string | null): Promise<CoordinationLookup> {
	const lookup = await fetchTarget(token, `/api/corsia-agent?token=${encodeURIComponent(token ?? '')}`);
	if (lookup.kind === 'target') corsiaAgentId.set(lookup.target.thread_id);
	else if (lookup.kind === 'absent') corsiaAgentId.set(null);
	return lookup;
}

/** Read the scope's designated thread: a project's conductor, or the workspace's CORSIA agent. */
export function fetchScopeTarget(token: string | null, scope: UiScope | null): Promise<CoordinationLookup> {
	return scope ? fetchConductor(token, scope) : fetchCorsiaAgent(token);
}
