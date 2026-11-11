import { writable } from 'svelte/store';

/**
 * The signed-in session. Kept apart from `auth.ts` so the shared request function can read it
 * without importing the store that itself makes requests.
 */
export const token = writable<string | null>(
	typeof localStorage !== 'undefined' ? localStorage.getItem('corsia_token') : null
);
