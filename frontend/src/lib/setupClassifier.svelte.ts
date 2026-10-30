export type ClassifierState = { state: 'idle' | 'downloading' | 'verifying' | 'self_testing' | 'ready' | 'failed'; bytes_done?: number; bytes_total?: number; interrupted?: boolean; code?: string; reason?: string; retryable?: boolean; bytes_needed?: number };

/** The one required background download. Setup never shows it and never waits on it: it starts, and a failure retries quietly. */
export function createSetupClassifier() {
	let state = $state<ClassifierState>({ state: 'idle' });

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
	async function start() {
		try {
			const response = await fetch('/api/setup/classifier', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
			if (response.ok) accept(await response.json());
		} catch { /* the next read reports what the server says */ }
	}

	// The setup window came back to the front: a failed download looks again. One stopped for space asks the
	// backend to recheck it; any other failure starts it again.
	async function retryIfFailed() {
		if (state.state !== 'failed') return;
		if (state.code !== 'disk_full') { await start(); return; }
		try {
			const response = await fetch('/api/setup/classifier/recheck', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
			if (response.ok) accept(await response.json());
		} catch { /* the next focus or the backend's own minute check looks again */ }
	}

	return { accept, read, start, retryIfFailed };
}
