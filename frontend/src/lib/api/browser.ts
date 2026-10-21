export class BrowserPaneApiError extends Error {
	constructor(readonly status: number, message: string) {
		super(message);
		this.name = 'BrowserPaneApiError';
	}
}

export async function openBrowserUrl(threadId: string, url: string, authToken: string): Promise<void> {
	const response = await fetch(`/api/browser/${threadId}/open?url=${encodeURIComponent(url)}`, {
		method: 'POST',
		headers: { Authorization: `Bearer ${authToken}` }
	});
	if (!response.ok) {
		throw new BrowserPaneApiError(response.status, await response.text());
	}
}
