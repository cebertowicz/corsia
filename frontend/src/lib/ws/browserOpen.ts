import { openBrowserPane } from '$lib/stores/artifactWorkspace';
import { on } from './events';

/** An agent opened a page: add its browser tab without taking the user's view. */
export function handleBrowserStarted(data: unknown): void {
	if (!data || typeof data !== 'object') return;
	const threadId = (data as Record<string, unknown>).thread_id;
	if (typeof threadId === 'string') openBrowserPane(threadId, true);
}

on('browser_started', handleBrowserStarted);
