/** Shared authenticated, conditional read boundary for both plan representations. */
import type { OperatorReadOutcome } from './plansTypes';

export type ReadOutcome<T> =
	| { status: 'fresh'; payload: T; etag: string }
	| Exclude<OperatorReadOutcome, { status: 'fresh' }>;

/** A reply this page cannot read (unparseable, or another contract version); the cause is unknown, so name only the next step. */
export const STALE_PAGE_MESSAGE = 'Couldn’t read this plan. Reload the page.';

export function safeErrorMessage(action: 'load' | 'update'): string {
	return action === 'load' ? 'Couldn’t load the plan. Try again.' : 'Couldn’t update the plan. Try again.';
}

/** A failed reply's body, or undefined when it is not JSON. */
export async function readJson(res: Response): Promise<unknown> {
	try {
		return await res.json();
	} catch {
		return undefined;
	}
}

/** The log reference the server attaches to its own failures; the only body field a failure shows. */
export function errorReference(body: unknown): string | undefined {
	const reference = (body as { reference?: unknown } | null)?.reference;
	return typeof reference === 'string' && reference.trim() ? reference : undefined;
}

/** A failure as the person reads it: the message, with the reference beside it when there is one; a narrow pane never splits the label from the code. */
export function errorText(outcome: { message: string; reference?: string }): string {
	return outcome.reference ? `${outcome.message} Reference:\u00a0${outcome.reference}` : outcome.message;
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
	} catch {
		return { status: 'error', message: safeErrorMessage('load') };
	}
	if (res.status === 304) return { status: 'not-modified' };
	if (res.status === 404) return { status: 'not-found' };
	if (res.status === 401) return { status: 'unauthorized' };
	if (res.status === 403) return { status: 'forbidden' };
	// Never surface a raw server body, which may contain internal detail.
	if (!res.ok) return { status: 'error', message: safeErrorMessage('load'), reference: errorReference(await readJson(res)) };
	const body = await readJson(res);
	// A body cut off or garbled in transit reads fine on a retry; one that parses but doesn't decode won't.
	if (body === undefined) return { status: 'error', message: safeErrorMessage('load') };
	const payload = decode(body);
	if (payload === null) return { status: 'error', message: STALE_PAGE_MESSAGE, reload: true };
	return { status: 'fresh', payload, etag: res.headers.get('ETag') ?? '' };
}
