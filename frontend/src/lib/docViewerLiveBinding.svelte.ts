import type { LiveDocumentSession } from '$lib/stores/liveDocumentSession';
import { placeLostOnLoad } from '$lib/docCanvasComments';

/** Lost comments whose quote is still in the text are placed again in a connected live load, and again when more are lost (`placeLostOnLoad`). */
export function placeLostWhenLive(reload: (artifactId: string) => boolean, load: () => object | undefined, artifactId: () => string | null, replaceableCount: () => number): void {
	const placeLost = placeLostOnLoad(reload);
	$effect(() => placeLost(load(), artifactId(), replaceableCount()));
}

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
