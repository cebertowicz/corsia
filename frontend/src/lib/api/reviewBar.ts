import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { fetchWithTimeout, COMMENT_POST_TIMEOUT_MS } from './docs';

export type ReviewVerdict = 'approved' | 'approved_with_changes' | 'changes_requested';

export interface FeedbackCounts { comments: number; suggestions: number; edits: number }

/** The review bar's read of one artifact: the newest raised ask and what the user did since it was raised. */
export interface ReviewBarRead {
	ask_raised: boolean;
	ask_reason: string | null;
	ask_raised_at: string | null;
	asker: { id: string; name: string } | null;
	other_askers: string[];
	other_asks: { name: string; reason: string }[];
	other_request_count: number;
	round_feedback_count: number;
	round_feedback_counts: FeedbackCounts;
}

export interface ReviewAnswer { verdict: ReviewVerdict; note: string | null; feedback_counts: FeedbackCounts }

/** The server's typed refusal of an answer; `code` is what the bar branches on. */
export class ReviewRefusal extends Error {
	constructor(readonly code: string, message: string) { super(message); }
}

const authorization = () => ({ Authorization: `Bearer ${get(token) ?? ''}` });

export async function fetchReviewBar(artifactId: string): Promise<ReviewBarRead> {
	const response = await fetch(`/api/attention/${encodeURIComponent(artifactId)}/review`, { headers: authorization() });
	if (!response.ok) throw new Error(`${response.status}`);
	return response.json();
}

/** Answer every raised ask on the artifact; a refusal that carries a code throws `ReviewRefusal`. */
export async function answerReview(artifactId: string, verdict: ReviewVerdict, note: string): Promise<ReviewAnswer> {
	const response = await fetchWithTimeout(`/api/attention/${encodeURIComponent(artifactId)}/clear`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ token: get(token) ?? '', outcome: 'answer', verdict, note: note.trim() || null }),
	}, COMMENT_POST_TIMEOUT_MS);
	if (response.ok) {
		const body = await response.json();
		return body.review;
	}
	const detail = await response.json().then((body) => body?.detail, () => undefined);
	if (detail && typeof detail === 'object' && typeof detail.code === 'string') {
		throw new ReviewRefusal(detail.code, String(detail.message ?? ''));
	}
	throw new Error(`${response.status}`);
}
