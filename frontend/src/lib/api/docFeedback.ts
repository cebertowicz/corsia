import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

export interface UndeliveredBatch {
	id: string;
	item_count: number;
	undelivered_reason: string;
	attempts: number;
}

export interface DocFeedbackResult {
	woken: string[];
	undelivered_batches: UndeliveredBatch[];
}

export class DocFeedbackError extends Error {
	constructor(readonly status: number) {
		super(`HTTP ${status}`);
	}
}

async function postFeedback(action: 'send' | 'leave' | 'return' | 'retry', artifactId: string, keepalive = false): Promise<DocFeedbackResult> {
	const res = await fetch(`/api/doc-feedback/${action}`, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${get(token) ?? ''}`,
			'Content-Type': 'application/json',
		},
		body: JSON.stringify({ artifact_id: artifactId }),
		keepalive,
	});
	if (!res.ok) throw new DocFeedbackError(res.status);
	return res.json();
}

export function sendDocFeedback(artifactId: string) {
	return postFeedback('send', artifactId);
}

export function leaveDocFeedback(artifactId: string) {
	return postFeedback('leave', artifactId, true);
}

/** The user is looking at the document: cancel leave grace. Reply markers clear per thread, through `/viewed`. */
export function returnDocFeedback(artifactId: string) {
	return postFeedback('return', artifactId);
}

export function retryDocFeedback(artifactId: string) {
	return postFeedback('retry', artifactId);
}
