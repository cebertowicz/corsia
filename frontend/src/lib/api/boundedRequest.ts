import { apiFetch, type ApiInit } from './request';

/** A rejected fetch for a comment request, distinguished from errors in application code. */
export class CommentConnectionError extends Error {
	constructor() { super('Comment connection unavailable'); }
}

/** Mark a browser fetch rejection at the request boundary, before application code reads the response. */
export function commentFetch(input: string, init: ApiInit): Promise<Response> {
	return apiFetch(input, init).catch((error: unknown) => {
		if (error instanceof TypeError) throw new CommentConnectionError();
		throw error;
	});
}

export class PostTimeoutError extends Error {
	constructor(ms: number) {
		super(`request timed out after ${ms}ms (may have committed server-side)`);
		this.name = 'PostTimeoutError';
	}
}

/**
 * How long a comment request may wait inside the browser before it is even sent. A browser holds a few connections per
 * origin (six over HTTP/1.1) and queues every other request until one frees; while slow reads hold them, a new request
 * waits its turn. On an install reached remotely from a phone (2026-11-26), requests waited up to about 50 s for a
 * connection. A comment's limits count this wait, so a post waiting its turn goes out instead of being cancelled unsent.
 */
const CONNECTION_WAIT_MS = 50_000;
/**
 * How long a comment's POST (and the document registration it waits on) has, from the call to its whole answer: the
 * connection wait above, plus 10 s for the server's own save (the slowest measured took 2.2 s, 2026-11-18). Cost: a
 * server that never answers takes this long to end the post failed.
 */
/** Allowance for the server's save, shared with the draft retry throttle. */
export const SERVER_SAVE_MS = 10_000;
export const COMMENT_POST_TIMEOUT_MS = CONNECTION_WAIT_MS + SERVER_SAVE_MS;
/**
 * How long the look for a comment an unanswered post may have stored waits before the post ends failed: the
 * connection wait above, since the look sits in the same queue as the post it follows, plus 5 s for the server (about
 * twice the slowest comment save, 2.2 s, and at most 47 ms of network, rounded up).
 */
export const COMMENT_LOOKUP_TIMEOUT_MS = CONNECTION_WAIT_MS + 5_000;
/**
 * The limit of the document review requests (the review bar and document reviews), from the call to the answer's
 * headers. It does not count the browser's connection wait, so a review request queued behind slow reads can end
 * failed before it is sent; giving these requests the comment's wait is outside the comment-post fix.
 */
export const REVIEW_REQUEST_TIMEOUT_MS = 12_000;

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
		return await Promise.race([commentFetch(input, { ...init, signal: ctrl.signal }).then(read), expired]);
	} catch (e) {
		if (e instanceof DOMException && e.name === 'AbortError') throw new PostTimeoutError(ms);
		throw e;
	} finally {
		clearTimeout(timer);
	}
}
