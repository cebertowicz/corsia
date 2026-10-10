/** Shared authenticated, conditional read boundary for both plan representations. */
import type { OperatorReadOutcome } from './plansTypes';

type ReadOutcome<T> =
	| { status: 'fresh'; payload: T; etag: string }
	| Exclude<OperatorReadOutcome, { status: 'fresh' }>;

export function safeErrorMessage(status: number): string {
	if (status >= 500) return 'The server had a problem handling this request.';
	return `Unable to load this scope (HTTP ${status}).`;
}

/** Bearer-auth GET headers, matching the existing api/attention.ts seam. */
export function authHeaders(bearerValue: string, etag: string | null): RequestInit {
	const headers: Record<string, string> = { Authorization: `Bearer ${bearerValue}` };
	if (etag) headers['If-None-Match'] = etag;
	return { headers };
}

export async function readPlan<T>(
	url: string,
	bearerValue: string,
	etag: string | null | undefined,
	decode: (body: unknown) => T | null,
): Promise<ReadOutcome<T>> {
	let res: Response;
	try {
		res = await fetch(url, authHeaders(bearerValue, etag ?? null));
	} catch (error) {
		return { status: 'error', message: error instanceof Error ? error.message : String(error) };
	}
	if (res.status === 304) return { status: 'not-modified' };
	if (res.status === 404) return { status: 'not-found' };
	if (res.status === 401) return { status: 'unauthorized' };
	if (res.status === 403) return { status: 'forbidden' };
	// Never surface a raw server body, which may contain internal detail.
	if (!res.ok) return { status: 'error', message: safeErrorMessage(res.status) };
	let body: unknown;
	try {
		body = await res.json();
	} catch {
		body = undefined;
	}
	const payload = body === undefined ? null : decode(body);
	if (payload === null) return { status: 'error', message: 'The server sent an unreadable reply.' };
	return { status: 'fresh', payload, etag: res.headers.get('ETag') ?? '' };
}
