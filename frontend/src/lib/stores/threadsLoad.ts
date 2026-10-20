import { get } from 'svelte/store';
import { token } from './auth';
import { reconcileThreadModelSnapshots } from './models';
import { threads, type Thread } from './threads';
import { uiScope } from './uiScope';
import { fetchScopeTarget } from '$lib/utils/conductor';

/** Refreshes the authoritative thread payload and its dependent scope target. */
export async function loadThreads(auth: string | null = get(token)): Promise<void> {
	if (!auth) return;
	const response = await fetch(`/api/threads?token=${encodeURIComponent(auth)}`);
	const data = await response.json();
	const refreshed = reconcileThreadModelSnapshots(data.threads as Thread[]);
	refreshed.sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());
	threads.set(refreshed);
	void fetchScopeTarget(auth, get(uiScope)); // the list omits the target; re-read its model too
}
