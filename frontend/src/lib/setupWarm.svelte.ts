import { fetchCorsiaAgent } from '$lib/utils/conductor';
import { getModelDispatchId } from '$lib/stores/models';
import { setupModelWorks, type SetupModel } from '$lib/setupModels';
import { on } from '$lib/ws/events';

type SkipReason = 'no_candidate' | 'agent_has_model' | 'first_run_not_pending' | 'warm_failed';

function logSkip(threadId: string, session: string, reason: SkipReason): void {
	const query = new URLSearchParams({ token: session, skip_reason: reason });
	void Promise.resolve().then(() => fetch(`/api/threads/${encodeURIComponent(threadId)}/warm?${query}`, { method: 'POST' })).catch(() => {});
}

type WarmProgressEvent = {
	type: 'thread_warm_progress';
	thread_id?: string;
	model?: string;
	progress?: { total_blocks?: number | null; ready_blocks?: number | null };
};

type FinishSetupWarmHandoff = {
	session: string | null;
	isFinishing: boolean;
	setFinishing: (finishing: boolean) => void;
	setSourceError: (error: string) => void;
	recordProgress: (session: string) => Promise<boolean>;
	getCatalog: () => SetupModel[];
	warm: Pick<ReturnType<typeof createSetupWarm>, 'start' | 'finish'>;
	exitMark: () => Promise<void> | undefined;
	onFinished: () => void;
};

/** Complete setup, join its local warm-up, then hand off to the first chat. */
export async function finishSetupWarmHandoff({
	session, isFinishing, setFinishing, setSourceError, recordProgress, getCatalog, warm, exitMark, onFinished,
}: FinishSetupWarmHandoff): Promise<void> {
	if (!session || isFinishing) return;
	setFinishing(true);
	setSourceError('');
	try {
		if (!await recordProgress(session)) {
			setSourceError('CORSIA couldn’t save setup progress. Try again.');
			return;
		}
		warm.start(getCatalog(), session);
		await warm.finish();
		await exitMark();
		onFinished();
	} finally {
		setFinishing(false);
	}
}

/** Warm a ready local candidate, without choosing the agent's model. */
export function createSetupWarm() {
	let running = $state(false);
	let percent = $state<number | null>(null);
	let attempted = '';
	let noCandidateSession: string | null = null;
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
		if (!session) { noCandidateSession = null; return; }
		if (!candidate) {
			if (noCandidateSession !== session) {
				noCandidateSession = session;
				void fetchCorsiaAgent(session).then((lookup) => {
					if (lookup.kind === 'target') logSkip(lookup.target.thread_id, session, 'no_candidate');
				}).catch(() => {});
			}
			return;
		}
		noCandidateSession = null;
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
				if (signal.aborted || lookup.kind !== 'target') return;
				if (lookup.target.model) {
					logSkip(lookup.target.thread_id, session, 'agent_has_model');
					return;
				}
				if (!lookup.firstRunPending) {
					logSkip(lookup.target.thread_id, session, 'first_run_not_pending');
					return;
				}
				stopSubscription = on('thread_warm_progress', (event: WarmProgressEvent) => {
					acceptProgress(event, lookup.target.thread_id, model);
				});
				const query = new URLSearchParams({ token: session, model, progress: 'true' });
				const url = `/api/threads/${encodeURIComponent(lookup.target.thread_id)}/warm?${query}`;
				try {
					const response = await fetch(`${url}&wait=true`, { method: 'POST', signal });
					const body: { ok?: boolean } | null = await response.json().catch(() => null);
					if (!signal.aborted && (!response.ok || !body || body.ok === false)) {
						logSkip(lookup.target.thread_id, session, 'warm_failed');
						console.warn('Setup warm did not finish', response.status);
					}
				} catch {
					if (!signal.aborted) {
						logSkip(lookup.target.thread_id, session, 'warm_failed');
						console.warn('Setup warm failed');
					}
				}
			} catch {
				if (!signal.aborted) console.warn('Setup warm failed');
			} finally { stopProgress(); running = false; }
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
