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

/**
 * The pending offer for this thread, or null when there is none. The offer is optional, so a failed
 * read shows the person nothing; it is reported to the console like the app's other background reads.
 */
export async function fetchSaveOffer(threadId: string): Promise<SaveOffer | null> {
	try {
		const response = await apiFetch(`/api/saved-logins/offer?thread_id=${encodeURIComponent(threadId)}`);
		if (!response.ok) {
			console.error('[saved login offer] read rejected', threadId, response.status);
			return null;
		}
		const offer = ((await response.json()) as { offer?: Partial<SaveOffer> | null }).offer;
		if (!offer) return null;
		if (typeof offer.domain !== 'string' || typeof offer.username !== 'string') {
			console.error('[saved login offer] malformed response', threadId);
			return null;
		}
		return { site: String(offer.site ?? ''), domain: offer.domain, username: offer.username, update: offer.update === true };
	} catch (error) {
		console.error('[saved login offer] read failed', threadId, error);
		return null;
	}
}

/** 409 means the offer was dropped or replaced meanwhile. Any other failure is shown to the person and also reported. */
export async function saveOffer(threadId: string): Promise<SaveResult> {
	try {
		const response = await post('save', threadId);
		if (response.ok) return 'saved';
		if (response.status === 409) return 'gone';
		console.error('[saved login offer] save rejected', threadId, response.status);
		return 'error';
	} catch (error) {
		console.error('[saved login offer] save failed', threadId, error);
		return 'error';
	}
}

/** The person said Not now, so the dock closes whatever the server answers; a failure is reported, not shown. */
export async function declineOffer(threadId: string): Promise<void> {
	try {
		const response = await post('not-now', threadId);
		if (!response.ok) console.error('[saved login offer] not-now rejected', threadId, response.status);
	} catch (error) {
		console.error('[saved login offer] not-now failed', threadId, error);
	}
}
