import { get, writable } from 'svelte/store';
import { acceptThreadModelTruth } from '$lib/stores/models';
import { activeThreadId } from '$lib/stores/threads';
import type { UiScope } from '$lib/stores/uiScope';

export interface ConductorTarget {
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

/** Result of one conductor lookup for a scope.
 *
 * `absent` is a confirmed answer: the scope truly has no conductor for this
 * caller. Transport failures, non-OK responses, and a missing token are
 * `unknown`, so callers can retry instead of rendering a false
 * "no conductor" state from a failed lookup. */
export type ConductorLookup =
	| { kind: 'target'; target: ConductorTarget; firstRunPending: boolean }
	| { kind: 'absent' }
	| { kind: 'unknown' };

/** The CORSIA agent's thread id once a lookup confirms it; its sources
 * panel shows only there (#3287). */
export const workspaceConductorId = writable<string | null>(null);

/** The conductor thread to land on at sign-in, or null to keep Activity.
 *
 * Only a pending first run in the workspace scope lands there, once per page
 * load, and never over a thread the user already opened (#3287 N2). */
export function firstRunLanding(
	lookup: ConductorLookup,
	{ scope, activeThreadId, landed }: { scope: UiScope | null; activeThreadId: string | null; landed: boolean },
): string | null {
	if (landed || scope || activeThreadId) return null;
	return lookup.kind === 'target' && lookup.firstRunPending ? lookup.target.thread_id : null;
}

let firstRunLanded = false;

/** Open the conductor once per page load when `firstRunLanding` says so. */
export function landOnFirstRun(
	lookup: ConductorLookup,
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

/** Read the scope's conductor target and accept its model as that thread's truth. */
export async function fetchConductor(
	token: string | null,
	scope: UiScope | null
): Promise<ConductorLookup> {
	if (!token) return { kind: 'unknown' };
	try {
		const workspace = conductorScopeParam(scope) === 'workspace';
		// The workspace scope's occupant is the CORSIA agent, with its own route.
		const url = workspace
			? `/api/corsia-agent?token=${encodeURIComponent(token)}`
			: `/api/conductor?token=${encodeURIComponent(token)}&scope=${encodeURIComponent(conductorScopeParam(scope))}`;
		const res = await fetch(url);
		if (!res.ok) return { kind: 'unknown' };
		const data = await res.json();
		const threadId = data?.target?.thread_id ?? data?.thread_id ?? null;
		if (!threadId) {
			if (workspace) workspaceConductorId.set(null);
			return { kind: 'absent' };
		}
		const target: ConductorTarget = { ...(data?.target ?? {}), thread_id: threadId };
		// The thread list omits the conductor, so this read is its only row: it
		// enters the same server-time ordering every listed thread's model does.
		if (target.model && target.updated_at) {
			acceptThreadModelTruth(threadId, target.model, target.updated_at);
		}
		if (workspace) workspaceConductorId.set(threadId);
		return { kind: 'target', target, firstRunPending: data?.first_run_pending === true };
	} catch {
		return { kind: 'unknown' };
	}
}
