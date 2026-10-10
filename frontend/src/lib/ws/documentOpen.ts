import { openAgentDocumentTab } from '$lib/stores/artifactWorkspace';
import { on } from './events';
import { getPresenceTracker } from './presence';
import { emitProductEvent } from '$lib/product_events';

interface DocumentOpenRequest {
	artifact_id: string;
	path: string;
	thread_id: string;
	title: string;
	type: 'document_open_request';
}

function isDocumentOpenRequest(data: unknown): data is DocumentOpenRequest {
	if (!data || typeof data !== 'object') return false;
	const frame = data as Record<string, unknown>;
	return frame.type === 'document_open_request'
		&& typeof frame.artifact_id === 'string'
		&& typeof frame.path === 'string'
		&& typeof frame.thread_id === 'string'
		&& typeof frame.title === 'string';
}

/** Apply a request only while this visible page still claims its target thread. */
export function handleDocumentOpenRequest(data: unknown): void {
	if (!isDocumentOpenRequest(data) || document.visibilityState !== 'visible') return;
	const presence = getPresenceTracker();
	if (!presence?.claimed || presence.claimedThreadId !== data.thread_id) return;
	if (!presence.documentOpenCapability?.canShow) {
		emitProductEvent('document', 'open_declined_on_receipt', {
			threadId: data.thread_id, artifactId: data.artifact_id,
		});
		return;
	}
	openAgentDocumentTab(data.thread_id, {
		artifactId: data.artifact_id,
		path: data.path,
		title: data.title,
	});
}

on('document_open_request', handleDocumentOpenRequest);
