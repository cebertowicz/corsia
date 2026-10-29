import { writable, derived, get } from 'svelte/store';

import { toasts } from '$lib/stores/toast';
import { allThreads } from '$lib/stores/threads';

interface User {
	id: string;
	name: string;
}

export const token = writable<string | null>(
	typeof localStorage !== 'undefined' ? localStorage.getItem('corsia_token') : null
);
export const user = writable<User | null>(null);
export const isAuthenticated = derived(user, $u => $u !== null);

token.subscribe(t => {
	if (!t) allThreads.set([]);
	if (typeof localStorage !== 'undefined') {
		if (t) localStorage.setItem('corsia_token', t);
		else localStorage.removeItem('corsia_token');
	}
});

/** Say so when sign-out cleared this browser but left the session usable. */
function warnSessionNotRevoked() {
	toasts.show(
		'Signed out here, but the server could not end the session. It stays valid until it expires — sign out again when you are back online.',
		'error',
		8000,
	);
}

export async function logout() {
	const currentToken = get(token);
	if (currentToken) {
		try {
			const response = await fetch('/api/auth/logout', {
				method: 'POST',
				headers: { Authorization: `Bearer ${currentToken}` },
			});
			// 401 means the session was already invalid, so nothing survives it.
			if (!response.ok && response.status !== 401) {
				console.warn('[auth] logout request failed', response.status);
				warnSessionNotRevoked();
			}
		} catch (error) {
			console.warn('[auth] logout request failed', error);
			warnSessionNotRevoked();
		}
	}
	token.set(null);
	user.set(null);
}
