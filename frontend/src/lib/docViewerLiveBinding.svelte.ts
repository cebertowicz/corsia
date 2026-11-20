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

/** A document opened awaiting its first save reloads once its draft has saved. */
export function reloadAfterLegacySave(awaiting: () => boolean, saved: () => boolean, done: () => void, reload: () => void): void {
	$effect(() => {
		if (!awaiting() || !saved()) return;
		done();
		reload();
	});
}

export function bindViewerSession(session: () => LiveDocumentSession | undefined, apply: (state: LiveDocumentSession | null) => void): void {
	$effect(() => {
		const current = session();
		if (!current) { apply(null); return; }
		return current.subscribe(apply);
	});
}
