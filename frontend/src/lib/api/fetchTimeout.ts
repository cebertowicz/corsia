import { apiFetch, type ApiInit } from './request';

export class PostTimeoutError extends Error {
	constructor(ms: number) {
		super(`request timed out after ${ms}ms (may have committed server-side)`);
		this.name = 'PostTimeoutError';
	}
}

export const COMMENT_POST_TIMEOUT_MS = 12_000;

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
