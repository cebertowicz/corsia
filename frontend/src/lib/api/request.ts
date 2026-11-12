import { get } from 'svelte/store';
import { token } from '$lib/stores/sessionToken';

/** What `apiFetch` accepts on top of `RequestInit`; the extra field never reaches `fetch`. */
export interface ApiInit extends RequestInit {
	/**
	 * Also name the session in the address, as the `token` query parameter. For routes that declare
	 * it as a required query parameter (a header alone answers 422 there). `apiFetch` sets it from
	 * the same value the header carries; a caller never builds that parameter itself.
	 */
	sessionInAddress?: boolean;
}

/**
 * The one way the app asks its own server for anything, and the one owner of the session on a
 * request.
 *
 * `apiFetch` reads the signed-in session and puts that single value in the `Authorization`
 * header, the form the Devices listener admits: an `/api` route outside its public list answers 401
 * `sign_in_required` without a person session, so a call that omits it works on the main computer
 * and silently fails on a phone or another computer. When `init.sessionInAddress` is set it puts the
 * same value in the address too, for routes that declare the session as a required query parameter.
 * Header and address therefore cannot differ (the listener refuses a request whose header and
 * address name different sessions), and no API function takes a session as an argument.
 *
 * A call made while signed out goes out unsigned, which is how sign-in itself is made. WebSocket
 * streams are outside this path: a browser cannot set a header on a socket, so they authenticate in
 * their first frame, which is what the listener expects from them.
 */
export function apiFetch(input: string, init?: ApiInit): Promise<Response> {
	const session = get(token);
	if (init === undefined) return session ? withSession(input, undefined, session, false) : fetch(input);
	const { sessionInAddress, ...rest } = init;
	return session ? withSession(input, rest, session, sessionInAddress === true) : fetch(input, rest);
}

function withSession(input: string, init: RequestInit | undefined, session: string, inAddress: boolean): Promise<Response> {
	const url = inAddress ? withSessionParameter(input, session) : input;
	const authorization = `Bearer ${session}`;
	const given = init?.headers;
	// A plain header object stays one, so a caller's own headers keep their names and case.
	if (!given || (!Array.isArray(given) && !(given instanceof Headers))) {
		return fetch(url, { ...init, headers: { ...given, Authorization: authorization } });
	}
	const headers = new Headers(given);
	headers.set('Authorization', authorization);
	return fetch(url, { ...init, headers });
}

/** The session goes first in the query, where every hand-built address used to put it. */
function withSessionParameter(input: string, session: string): string {
	const named = `token=${encodeURIComponent(session)}`;
	const at = input.indexOf('?');
	if (at < 0) return `${input}?${named}`;
	const rest = input.slice(at + 1);
	return rest ? `${input.slice(0, at + 1)}${named}&${rest}` : `${input}${named}`;
}
