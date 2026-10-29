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
	const fix = localHostRefusal(body) ?? (typeof body?.detail?.fix === 'string' ? body.detail.fix : typeof body?.fix === 'string' ? body.fix : undefined);
	if (response.ok && body?.ok !== false) return { result: 'started', reference };
	const detail = body?.detail && typeof body.detail === 'object' ? body.detail : body;
	const reason = typeof detail?.reason === 'string' ? detail.reason : undefined;
	return { result: 'failed', reference, fix: fix ?? LOCAL_START_NETWORK_FAILURE, reason };
}

/** The backend's own account refusal names the cause; a validation 422 has no string detail. */
export function accountRefusal(detail: unknown): string | null { return typeof detail === 'string' && detail.trim() ? detail.trim() : null; }

export function passwordConfirmationError(password: string, confirmation: string): string { return confirmation && password !== confirmation ? 'Passwords don’t match.' : ''; }
