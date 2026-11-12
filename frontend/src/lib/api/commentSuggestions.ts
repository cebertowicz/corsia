import type { ServerComment } from './docs';
import { apiFetch } from './request';

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
	const response = await apiFetch(`/api/comments/${commentId}/decision`, {
		sessionInAddress: true,
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ decision, all_from_author: allFromAuthor }),
	});
	return decided(response, `Couldn't ${decision} this suggestion`);
}

/** Accept every open suggestion on a document, from every author, in one write; having none is not an error. */
export async function acceptAllSuggestions(artifactId: string): Promise<SuggestionDecision> {
	const response = await apiFetch(`/api/artifacts/${encodeURIComponent(artifactId)}/suggestions/accept-all`, { sessionInAddress: true, method: 'POST' });
	return decided(response, "Couldn't accept the suggestions");
}

async function decided(response: Response, fallback: string): Promise<SuggestionDecision> {
	if (!response.ok) {
		let detail = fallback, kind: string | null = null;
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
