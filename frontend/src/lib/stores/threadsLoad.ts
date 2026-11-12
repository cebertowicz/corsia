import { get } from 'svelte/store';
import { token } from './auth';
import { reconcileThreadModelSnapshots } from './models';
import { threads, coordinationThreads, type Thread } from './threads';
import { uiScope } from './uiScope';
import { fetchScopeTarget } from '$lib/utils/conductor';
import { apiFetch } from '$lib/api/request';

/** Refreshes the authoritative thread payload and its dependent scope target. */
export async function loadThreads(): Promise<void> {
	const tokenAtStart = get(token);
	if (!tokenAtStart) return;
	const response = await apiFetch(`/api/threads`, { sessionInAddress: true });
	const data = await response.json();
	if (get(token) !== tokenAtStart) return;
	const refreshed = reconcileThreadModelSnapshots(data.threads as Thread[]);
	refreshed.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
	threads.set(refreshed);
	coordinationThreads.set(reconcileThreadModelSnapshots((data.coordination ?? []) as Thread[]));
	void fetchScopeTarget(get(uiScope)); // the list omits the target; re-read its model too
}
