import { apiFetch } from './request';
export class BrowserPaneApiError extends Error {
	constructor(readonly status: number, message: string) {
		super(message);
		this.name = 'BrowserPaneApiError';
	}
}

export async function openBrowserUrl(threadId: string, url: string): Promise<void> {
	const response = await apiFetch(`/api/browser/${threadId}/open?url=${encodeURIComponent(url)}`, {
		method: 'POST',
	});
	if (!response.ok) {
		throw new BrowserPaneApiError(response.status, await response.text());
	}
}
