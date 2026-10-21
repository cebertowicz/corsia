import type { SetupJob } from '$lib/components/SetupShell.svelte';

export type ClassifierState = { state: 'idle' | 'downloading' | 'verifying' | 'self_testing' | 'ready' | 'failed'; bytes_done?: number; bytes_total?: number; interrupted?: boolean; code?: string; reason?: string; retryable?: boolean; bytes_needed?: number };

/** The rail's only words for this download: a person is never told what it is, only that setup is finishing. */
export const FINISHING_SETUP = 'Finishing setup';

/** The one required background download as the Setup rail shows it: a job while it runs or fails, nothing otherwise. */
export function createSetupClassifier() {
	let state = $state<ClassifierState>({ state: 'idle' });
	const percent = $derived(state.state === 'downloading' && state.bytes_total ? Math.min(100, Math.floor(100 * (state.bytes_done ?? 0) / state.bytes_total)) : undefined);
	const meta = $derived(
		state.state === 'downloading' ? (state.interrupted ? 'Reconnecting' : percent == null ? 'Downloading' : `Downloading · ${percent}%`) : 'Checking',
	);

	function accept(next: ClassifierState) {
		if (typeof next?.state === 'string') state = next;
	}

	// Before the first account there is no socket: the state is read on setup's own events,
	// and from then on the backend nudges with `classifier_updated`.
	async function read() {
		try {
			const response = await fetch('/api/setup/classifier');
			if (response.ok) accept(await response.json());
		} catch { /* the next event or reconnect reads again */ }
	}

	// Starts, joins or retries the one download; the backend keeps it going whether or not this screen stays open.
	// A retry reads as downloading at once, so the dot is never red while one is in flight.
	async function start() {
		if (state.state === 'failed') state = { state: 'downloading', bytes_done: 0, bytes_total: 0 };
		try {
			const response = await fetch('/api/setup/classifier', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
			if (response.ok) accept(await response.json());
		} catch { /* the next read reports what the server says */ }
	}

	// The setup window came back to the front: a download stopped for space looks again, and resumes if there is room.
	async function recheckSpace() {
		if (state.state !== 'failed' || state.code !== 'disk_full') return;
		try {
			const response = await fetch('/api/setup/classifier/recheck', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
			if (response.ok) accept(await response.json());
		} catch { /* the next focus or the backend's own minute check looks again */ }
	}

	const job = $derived<SetupJob | undefined>(
		state.state === 'ready' || state.state === 'idle' ? undefined
			: state.state === 'failed'
				? { label: FINISHING_SETUP, state: 'error', meta: state.reason || 'The download needs attention.', action: { label: 'Retry', onclick: start } }
				: { label: FINISHING_SETUP, state: 'busy', meta, progress: percent == null ? undefined : percent / 100 },
	);

	return {
		get job() { return job; },
		accept, read, start, recheckSpace,
	};
}
