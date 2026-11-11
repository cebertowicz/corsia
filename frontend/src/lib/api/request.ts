import { get } from 'svelte/store';
import { token } from '$lib/stores/sessionToken';

/**
 * The one way the app asks its own server for anything.
 *
 * Every call carries the signed-in session in the `Authorization` header, the form the Devices
 * listener admits: an `/api` route outside its public list answers 401 `sign_in_required` without
 * a person session, so a call that omits it works on the main computer and silently fails on a
 * phone or another computer. The session is never put in the URL here — a URL-borne credential
 * reaches server logs, referrers and browser history, and a request that names two different
 * sessions is refused rather than guessed at.
 *
 * A call made before sign-in has no session to send and goes out unsigned, exactly as it did
 * before. WebSocket streams are outside this path: a browser cannot set a header on a socket, so
 * they authenticate in their first frame, which is what the listener expects from them.
 */
export function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
	const headers = new Headers(init.headers);
	const session = get(token);
	if (session && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${session}`);
	return fetch(input, { ...init, headers });
}
