import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';

export function reopenOnReconnect(connected: () => boolean, refused: () => boolean, reopen: () => void): void {
	let wasConnected = false;
	$effect(() => {
		const nowConnected = connected();
		const shouldReopen = nowConnected && !wasConnected && refused();
		wasConnected = nowConnected;
		if (shouldReopen) reopen();
	});
}

export function bindViewerSession(session: () => LiveDocumentSession | undefined, apply: (state: LiveDocumentSession | null) => void): void {
	$effect(() => {
		const current = session();
		if (!current) { apply(null); return; }
		return current.subscribe(apply);
	});
}
