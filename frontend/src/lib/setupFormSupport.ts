import { localHostRefusal } from './localHostRefusal';

type LocalStartResponse = { result: 'started' | 'failed'; reference?: string; fix?: string; reason?: string };

export const LOCAL_START_NETWORK_FAILURE = 'CORSIA couldn’t start local setup.';

export async function requestLocalStart(session: string | null): Promise<LocalStartResponse> {
	const response = await fetch(session ? '/api/providers/local/setup' : '/api/setup/local-download', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', ...(session ? { Authorization: `Bearer ${session}` } : {}) },
		body: JSON.stringify(session ? { onboarding: true } : {}),
	});
	const body = await response.json().catch(() => null);
	const reference = typeof body?.detail?.ref === 'string' ? body.detail.ref : typeof body?.ref === 'string' ? body.ref : undefined;
	if (response.ok && body?.ok !== false) return { result: 'started', reference };
	const detail = body?.detail && typeof body.detail === 'object' ? body.detail : body;
	const reason = typeof detail?.reason === 'string' ? detail.reason : undefined;
	const knownFixes = [
		'Quit CORSIA completely, reopen it, then press Retry.',
		'The CORSIA app is damaged because its bundled oMLX executable is missing. Download CORSIA again, replace this app, then try again.',
		'Check your internet connection, then try again.',
		'Choose a subscription or API provider instead.',
		'The oMLX app already on this computer is out of date or broken. Update it, then press Retry.',
		'CORSIA couldn’t set up the local model.',
	];
	// Only the backend's fixed recovery templates are safe to display; never echo arbitrary failure text.
	const safeFix = typeof detail?.fix === 'string' && (
		knownFixes.includes(detail.fix) ||
		/^Free up \d{1,9}(?:\.\d{1,2})? GB, then try again\.$/.test(detail.fix) ||
		/^The local model needs (?:\d{1,9}\.\d GB more|more) free space\. Free it up, then try again\.$/.test(detail.fix)
	) ? detail.fix : undefined;
	const fix = localHostRefusal(body) ?? safeFix ?? LOCAL_START_NETWORK_FAILURE;
	return { result: 'failed', reference, fix, reason };
}

/** The backend's own account refusal names the cause; a validation 422 has no string detail. */
export function accountRefusal(detail: unknown): string | null { return typeof detail === 'string' && detail.trim() ? detail.trim() : null; }

export function passwordConfirmationError(password: string, confirmation: string): string { return confirmation && password !== confirmation ? 'Passwords don’t match.' : ''; }
