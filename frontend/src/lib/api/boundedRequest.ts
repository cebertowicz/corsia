import { apiFetch, type ApiInit } from './request';

export class PostTimeoutError extends Error {
	constructor(ms: number) {
		super(`request timed out after ${ms}ms (may have committed server-side)`);
		this.name = 'PostTimeoutError';
	}
}

export const COMMENT_POST_TIMEOUT_MS = 12_000;
/**
 * How long the look for a comment an unanswered post may have stored waits before the post ends failed. Basis: the
 * slowest comment save measured on a production install (2026-11-18) took 2.2 s on the server, and the network added
 * at most 47 ms. The look gets about twice that, 4.5 s, rounded up to 5 s.
 */
export const COMMENT_LOOKUP_TIMEOUT_MS = 5_000;

/** The answer's headers inside `ms`; reading its body is the caller's, unbounded. */
export async function fetchWithTimeout(input: string, init: ApiInit, ms: number): Promise<Response> {
	const ctrl = new AbortController();
	const timer = setTimeout(() => ctrl.abort(), ms);
	try {
		return await apiFetch(input, { ...init, signal: ctrl.signal });
	} catch (e) {
		if (e instanceof DOMException && e.name === 'AbortError') throw new PostTimeoutError(ms);
		throw e;
	} finally {
		clearTimeout(timer);
	}
}

/**
 * The answer and `read` of its body inside one deadline of `ms`: a body that stalls ends the request as an answer that
 * never comes does, with `PostTimeoutError`, whether or not the body read honours the abort.
 */
export async function readWithin<T>(input: string, init: ApiInit, ms: number, read: (response: Response) => Promise<T>): Promise<T> {
	const ctrl = new AbortController();
	let timer: ReturnType<typeof setTimeout> | undefined;
	const expired = new Promise<never>((_, reject) => {
		timer = setTimeout(() => { ctrl.abort(); reject(new PostTimeoutError(ms)); }, ms);
	});
	try {
		return await Promise.race([apiFetch(input, { ...init, signal: ctrl.signal }).then(read), expired]);
	} catch (e) {
		if (e instanceof DOMException && e.name === 'AbortError') throw new PostTimeoutError(ms);
		throw e;
	} finally {
		clearTimeout(timer);
	}
}
