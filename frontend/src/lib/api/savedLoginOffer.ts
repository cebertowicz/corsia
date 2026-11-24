import { apiFetch } from './request';

/** What the server holds for a sign-in the person just made in this pane. It never carries a password. */
export interface SaveOffer {
	site: string;
	domain: string;
	username: string;
	update: boolean;
}

export type SaveResult = 'saved' | 'gone' | 'error';

const post = (path: string, threadId: string) => apiFetch(`/api/saved-logins/offer/${path}`, {
	method: 'POST',
	headers: { 'Content-Type': 'application/json' },
	body: JSON.stringify({ thread_id: threadId }),
});

/** The pending offer for this thread, or null when there is none or the read fails. */
export async function fetchSaveOffer(threadId: string): Promise<SaveOffer | null> {
	try {
		const response = await apiFetch(`/api/saved-logins/offer?thread_id=${encodeURIComponent(threadId)}`);
		if (!response.ok) return null;
		const offer = ((await response.json()) as { offer?: Partial<SaveOffer> | null }).offer;
		if (!offer || typeof offer.domain !== 'string' || typeof offer.username !== 'string') return null;
		return { site: String(offer.site ?? ''), domain: offer.domain, username: offer.username, update: offer.update === true };
	} catch {
		return null;
	}
}

/** 409 means the offer was dropped or replaced meanwhile. */
export async function saveOffer(threadId: string): Promise<SaveResult> {
	try {
		const response = await post('save', threadId);
		if (response.ok) return 'saved';
		return response.status === 409 ? 'gone' : 'error';
	} catch {
		return 'error';
	}
}

export async function declineOffer(threadId: string): Promise<void> {
	try { await post('not-now', threadId); } catch { /* the dock closes either way */ }
}
