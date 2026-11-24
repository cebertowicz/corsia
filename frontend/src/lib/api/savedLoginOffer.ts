import { apiFetch } from './request';

/** What the server holds for a sign-in the person just made in this pane. It never carries a password. */
export interface SaveOffer {
	site: string;
	domain: string;
	username: string;
	update: boolean;
}

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

/**
 * Saved, or refused. A refusal other than a bad request (400) or a server fault (5xx) carries the server's own
 * sentence in the body's `detail`, which is what the person reads; any other failure, a network one included, has
 * no message here, and the pane says its own line. Every failure is also reported to the console.
 */
export type SaveResult = { saved: true } | { saved: false; message: string | null };

export async function saveOffer(threadId: string): Promise<SaveResult> {
	try {
		const response = await post('save', threadId);
		if (response.ok) return { saved: true };
		console.error('[saved login offer] save rejected', threadId, response.status);
		if (response.status === 400 || response.status >= 500) return { saved: false, message: null };
		return { saved: false, message: await serverText(response) };
	} catch (error) {
		console.error('[saved login offer] save failed', threadId, error);
		return { saved: false, message: null };
	}
}

async function serverText(response: Response): Promise<string | null> {
	try {
		const { detail } = (await response.json()) as { detail?: unknown };
		return typeof detail === 'string' && detail.trim() ? detail : null;
	} catch {
		return null;
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
