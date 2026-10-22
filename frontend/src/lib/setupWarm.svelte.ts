import { fetchCorsiaAgent } from '$lib/utils/conductor';
import { getModelDispatchId } from '$lib/stores/models';
import { setupModelWorks, type SetupModel } from '$lib/setupModels';
import { on } from '$lib/ws/events';

type WarmProgressEvent = {
	type: 'thread_warm_progress';
	thread_id?: string;
	model?: string;
	progress?: { total_blocks?: number | null; ready_blocks?: number | null };
};

/** Warm a ready local candidate, without choosing the agent's model. */
export function createSetupWarm() {
	let running = $state(false);
	let percent = $state<number | null>(null);
	let attempted = '';
	let pending: Promise<void> | null = null;
	let controller: AbortController | null = null;
	let stopSubscription: (() => void) | undefined;

	function stopProgress() {
		stopSubscription?.();
		stopSubscription = undefined;
	}

	function acceptProgress(event: WarmProgressEvent, threadId: string, model: string) {
		if (event.thread_id !== threadId || event.model !== model) return;
		const total = event.progress?.total_blocks;
		const ready = event.progress?.ready_blocks;
		percent = typeof total === 'number' && Number.isFinite(total) && total > 0
			&& typeof ready === 'number' && Number.isFinite(ready) && ready >= 0
			? Math.min(99, Math.floor(100 * ready / total))
			: null;
	}

	function start(catalog: SetupModel[], session: string | null): void {
		const candidate = catalog.find((row) => row.connection === 'omlx' && row.backend === 'network' && setupModelWorks(row));
		if (!session || !candidate) return;
		const model = getModelDispatchId(candidate);
		if (!model || attempted === model || running) return;
		attempted = model;
		running = true;
		percent = null;
		controller = new AbortController();
		const signal = controller.signal;
		pending = (async () => {
			try {
				const lookup = await fetchCorsiaAgent(session);
				if (signal.aborted || lookup.kind !== 'target' || lookup.target.model || !lookup.firstRunPending) return;
				stopSubscription = on('thread_warm_progress', (event: WarmProgressEvent) => {
					acceptProgress(event, lookup.target.thread_id, model);
				});
				const query = new URLSearchParams({ token: session, model });
				const url = `/api/threads/${encodeURIComponent(lookup.target.thread_id)}/warm?${query}`;
				await fetch(`${url}&wait=true`, { method: 'POST', signal });
			} catch { /* An unreachable local model leaves the normal cold-turn path available. */ }
			finally { stopProgress(); running = false; }
		})();
	}

	return {
		get running() { return running; },
		get percent() { return percent; },
		start,
		async finish() { await pending; },
		destroy() { controller?.abort(); stopProgress(); },
	};
}
