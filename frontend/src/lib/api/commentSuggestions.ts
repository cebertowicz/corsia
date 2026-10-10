import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import type { ServerComment } from './docs';

export interface SuggestionDecision {
	comment?: ServerComment;
	accepted?: string[];
	skipped?: { id: string; reason: string }[];
	text?: string;
	revision?: string;
}

/**
 * The server answered and turned the decision down. A request that never got an answer throws fetch's own error instead.
 * `kind` is the server's reason (`stale`, `decided`, `unrecorded`, …); several share one status, so it is what to act on.
 */
export class SuggestionRefused extends Error {
	constructor(message: string, readonly status: number, readonly kind: string | null = null) {
		super(message);
	}
}

export async function decideSuggestion(
	commentId: string, decision: 'accept' | 'reject', allFromAuthor = false
): Promise<SuggestionDecision> {
	const response = await fetch(`/api/comments/${commentId}/decision?token=${encodeURIComponent(get(token) ?? '')}`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ decision, all_from_author: allFromAuthor }),
	});
	if (!response.ok) {
		let detail = `Couldn't ${decision} this suggestion`, kind: string | null = null;
		try {
			const body = await response.json();
			if (typeof body.detail === 'string') detail = body.detail;
			else if (body.detail && typeof body.detail.line === 'string') detail = body.detail.line;
			if (typeof body.kind === 'string') kind = body.kind;
		} catch { /* Preserve the useful local fallback for non-JSON errors. */ }
		throw new SuggestionRefused(detail, response.status, kind);
	}
	return response.json();
}
