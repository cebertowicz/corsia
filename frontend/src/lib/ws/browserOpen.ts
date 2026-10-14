import { openBrowserPane } from '$lib/stores/artifactWorkspace';
import { on } from './events';

/** An agent opened a shared browser: its first reveal takes the view; later ones add a missing tab in the background. */
export function handleBrowserOpened(data: unknown): void {
	if (!data || typeof data !== 'object') return;
	const threadId = (data as Record<string, unknown>).thread_id;
	if (typeof threadId === 'string') openBrowserPane(threadId, true);
}

on('browser_opened', handleBrowserOpened);
