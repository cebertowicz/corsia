import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

export type DeliveryPolicy = 'immediate' | 'document-held' | 'image-held';

export interface UndeliveredBatch {
	id: string;
	item_count: number;
	undelivered_reason: string;
	attempts: number;
	delivery_policy: DeliveryPolicy;
}

/** The unsent comments left on a document: the total and each held lane. */
export interface DocFeedbackCounts {
	open_count: number;
	document_held_count: number;
	image_held_count: number;
}

export interface DocFeedbackResult extends DocFeedbackCounts {
	woken: string[];
	undelivered_batches: UndeliveredBatch[];
	/** How many comments this call closed; 0 when another sender closed them first. */
	sent_count?: number;
}

/** A thread's document-held comments, one row per held document. */
export interface HeldThreadFeedback {
	open_count: number;
	artifacts: { artifact_id: string; open_count: number }[];
	/** Only on a send: how many comments it closed. */
	sent_count?: number;
}

export class DocFeedbackError extends Error {
	constructor(readonly status: number) {
		super(`HTTP ${status}`);
	}
}

async function postFeedback(
	action: 'send' | 'retry', artifactId: string, keepalive = false, deliveryPolicy?: DeliveryPolicy,
): Promise<DocFeedbackResult> {
	const res = await fetch(`/api/doc-feedback/${action}`, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${get(token) ?? ''}`,
			'Content-Type': 'application/json',
		},
		body: JSON.stringify({ artifact_id: artifactId, ...(deliveryPolicy ? { delivery_policy: deliveryPolicy } : {}) }),
		keepalive,
	});
	if (!res.ok) throw new DocFeedbackError(res.status);
	return res.json();
}

/** Without a policy this closes every lane of the document (closing its tab); with one, only that lane. */
export function sendDocFeedback(artifactId: string, deliveryPolicy?: DeliveryPolicy, keepalive = false) {
	return postFeedback('send', artifactId, keepalive, deliveryPolicy);
}

export function retryDocFeedback(artifactId: string, deliveryPolicy: DeliveryPolicy) {
	return postFeedback('retry', artifactId, false, deliveryPolicy);
}

export async function touchDocFeedback(artifactId: string, deliveryPolicy: 'document-held' | 'image-held') {
	const res = await fetch('/api/doc-feedback/touch', {
		method: 'POST',
		headers: { Authorization: `Bearer ${get(token) ?? ''}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({ artifact_id: artifactId, delivery_policy: deliveryPolicy }),
	});
	if (!res.ok) throw new DocFeedbackError(res.status);
	return res.json() as Promise<{ touched: number }>;
}

export async function heldDocFeedbackForThread(threadId: string): Promise<HeldThreadFeedback> {
	const res = await fetch(`/api/doc-feedback/thread/${threadId}`, { headers: { Authorization: `Bearer ${get(token) ?? ''}` } });
	if (!res.ok) throw new DocFeedbackError(res.status);
	return res.json();
}

export async function sendHeldDocFeedbackForThread(threadId: string): Promise<HeldThreadFeedback> {
	const res = await fetch('/api/doc-feedback/thread/send', {
		method: 'POST',
		headers: { Authorization: `Bearer ${get(token) ?? ''}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({ thread_id: threadId }),
	});
	if (!res.ok) throw new DocFeedbackError(res.status);
	return res.json();
}
