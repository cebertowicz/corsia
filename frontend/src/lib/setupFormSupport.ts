import type { SetupStep } from '$lib/components/SetupShell.svelte';
import { localHostRefusal } from './localHostRefusal';

type LocalStartResponse = { result: 'started' | 'failed'; reference?: string; fix?: string };

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
	if (!fix) throw new Error('local start response omitted recovery');
	return { result: 'failed', reference, fix };
}

/** The backend's own account refusal names the cause; a validation 422 has no string detail. */
export function accountRefusal(detail: unknown): string | null { return typeof detail === 'string' && detail.trim() ? detail.trim() : null; }

export function passwordConfirmationError(password: string, confirmation: string): string { return confirmation && password !== confirmation ? 'Passwords don’t match.' : ''; }

/** The setup progress rail for every page except the remote-connect one, which brings its own steps. */
export function setupSteps(page: string, signedIn: boolean): SetupStep[] {
	return [
		{ label: 'Getting started', icon: 'computer', state: ['checking', 'welcome', 'hard-stop'].includes(page) || (page === 'error' && !signedIn) ? 'current' : 'done' },
		{ label: 'Account & workspace', icon: 'person', state: page === 'account' ? 'current' : ['subscriptions', 'models'].includes(page) || (page === 'error' && signedIn) ? 'done' : 'todo' },
		{ label: 'Your subscriptions', icon: 'card', state: page === 'subscriptions' ? 'current' : page === 'models' ? 'done' : 'todo' },
		{ label: 'Providers & models', icon: 'chip', state: page === 'models' ? 'current' : 'todo' },
	];
}
