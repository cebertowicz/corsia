import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { fetchWithTimeout, COMMENT_POST_TIMEOUT_MS } from './docs';

export type ReviewVerdict = 'approved' | 'approved_with_changes' | 'changes_requested';

export interface FeedbackCounts { comments: number; suggestions: number; edits: number }

/** The person step whose instructions this document is: the bar offers its Done / Won't do. */
export interface PersonStepInstruction {
	node_id: string;
	node_title: string;
	node_revision: number;
	plan_id: string;
	owner_thread_id: string;
	owner_name: string;
}

/**
 * One open review request on the artifact. `purpose: 'send'` is set by the request's producer and marks the request an
 * email draft's own Send and Don't send answer; any other request is an ordinary document review.
 * PROTOTYPE: the server sends neither `requests` nor `purpose` yet (a dependency on Structured Plans); a read without
 * `requests` is one ordinary ask, as before.
 */
export interface ReviewRequest {
	hold_id: string;
	asker: { id: string; name: string };
	reason: string;
	purpose?: 'send';
}

/** The review bar's read of one artifact: the newest raised ask and what the user did since it was raised. */
export interface ReviewBarRead {
	ask_raised: boolean;
	/** The open requests, newest first. Absent: the read is one ordinary ask. */
	requests?: ReviewRequest[];
	/** The Plans-owned holds this answer closes; each one identifies one review round. */
	hold_ids: string[];
	ask_reason: string | null;
	ask_raised_at: string | null;
	asker: { id: string; name: string } | null;
	other_askers: string[];
	other_asks: { name: string; reason: string }[];
	other_request_count: number;
	round_feedback_count: number;
	round_feedback_counts: FeedbackCounts;
	person_step?: PersonStepInstruction;
}

export interface ReviewAnswer { verdict: ReviewVerdict; note: string | null; feedback_counts: FeedbackCounts }

/** The server's typed refusal of an answer; `code` is what the bar branches on. */
export class ReviewRefusal extends Error {
	constructor(readonly code: string, message: string) { super(message); }
}

/**
 * The holds the canvas's review answers: every ordinary request, and a send request only once the email has ended (`emailEnded`):
 * before that its answers are Send and Don't send, and after it nothing else can answer it.
 */
export function answerHoldIds(read: ReviewBarRead | null, emailEnded = false): string[] {
	if (!read) return [];
	return read.requests ? read.requests.filter((request) => emailEnded || request.purpose !== 'send').map((request) => request.hold_id) : read.hold_ids;
}

/** The holds Send and Don't send answer: the requests their producer marked `purpose: 'send'`, and nothing else. */
export function sendHoldIds(read: ReviewBarRead | null): string[] {
	return read?.requests?.filter((request) => request.purpose === 'send').map((request) => request.hold_id) ?? [];
}

const authorization = () => ({ Authorization: `Bearer ${get(token) ?? ''}` });

export async function fetchReviewBar(artifactId: string): Promise<ReviewBarRead> {
	const response = await fetch(`/api/attention/${encodeURIComponent(artifactId)}/review`, { headers: authorization() });
	if (!response.ok) throw new Error(`${response.status}`);
	return response.json();
}

/**
 * Answer the raised asks the bar showed (`holdIds`); the server refuses as stale once a newer ask replaced them.
 * A refusal that carries a code throws `ReviewRefusal`.
 * The session also rides in the Authorization header: the Devices listener admits an `/api` request only by its
 * header or query credential, so a body-only token is refused there before the route reads it.
 */
export async function answerReview(artifactId: string, verdict: ReviewVerdict, note: string, holdIds: string[]): Promise<ReviewAnswer> {
	const response = await fetchWithTimeout(`/api/attention/${encodeURIComponent(artifactId)}/clear`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', ...authorization() },
		body: JSON.stringify({ token: get(token) ?? '', outcome: 'answer', verdict, note: note.trim() || null, hold_ids: holdIds }),
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
