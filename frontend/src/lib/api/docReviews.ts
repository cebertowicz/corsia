import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { ok, fetchWithTimeout, COMMENT_POST_TIMEOUT_MS } from './docs';


export interface DocumentReview {
	id: string;
	reviewer_thread_id: string;
	reviewer_name: string;
	requester_name: string;
	created_at: string;
	may_cancel: boolean;
	outcome: 'open' | 'ended_without_comments' | 'failed';
}
export interface DocumentReviewer {
	id: string;
	name: string;
	responsibility: string;
	requested: boolean;
	unavailable_reason: string | null;
	unavailable_text: string | null;
}
export interface DocumentReviews {
	reviews: DocumentReview[];
	reviewers?: DocumentReviewer[];
	reviewers_error?: 'roster_error';
}
function headers() { return { Authorization: `Bearer ${get(token) ?? ''}`, 'Content-Type': 'application/json' }; }
export async function fetchDocumentReviews(artifactId: string, threadId: string | null): Promise<DocumentReviews> {
	const query = threadId ? `?thread_id=${encodeURIComponent(threadId)}` : '';
	return ok(await fetch(`/api/artifacts/${encodeURIComponent(artifactId)}/reviews${query}`, { headers: headers() }));
}
export async function requestDocumentReview(artifactId: string, threadId: string, reviewer: string, note: string, clientId: string): Promise<void> {
	const response = await fetchWithTimeout('/api/doc-reviews', {
		method: 'POST', headers: headers(),
		body: JSON.stringify({ artifact_id: artifactId, thread_id: threadId, reviewer, note: note || null, client_id: clientId }),
	}, COMMENT_POST_TIMEOUT_MS);
	await ok(response);
}
export async function cancelDocumentReview(id: string): Promise<void> {
	await ok(await fetchWithTimeout(`/api/doc-reviews/${encodeURIComponent(id)}`, { method: 'DELETE', headers: headers() }, COMMENT_POST_TIMEOUT_MS));
}
/** Plain refusals from the canonical service; transport failures get local recovery copy. */
export function reviewFailure(error: unknown, fallback: string): string {
	if (!(error instanceof Error)) return fallback;
	const match = /^\d{3} (.+)$/.exec(error.message);
	if (!match) return fallback;
	try {
		const detail = JSON.parse(match[1]).detail;
		return typeof detail === 'string' ? detail : fallback;
	} catch { return fallback; /* A non-JSON transport response must not appear as server markup. */ }
}
