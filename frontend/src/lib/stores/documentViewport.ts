import { get } from 'svelte/store';
import type { ViewingHeading } from '../cm/viewing-heading';
import { getActiveTab, threadWorkspaces } from './artifactWorkspace';

/** Mounted editors supply a read at send time; scrolls never emit messages or persist state. */
const headingReaders = new Map<string, () => ViewingHeading | null>();

export function registerDocumentViewport(instanceId: string, heading: () => ViewingHeading | null): () => void {
	headingReaders.set(instanceId, heading);
	return () => {
		if (headingReaders.get(instanceId) === heading) headingReaders.delete(instanceId);
	};
}

export interface ViewingDocument {
	artifact_id: string;
	/** One-based source line of the heading, resolved to text by the server. */
	heading_anchor?: number;
	/** The heading line as the editor shows it. The server only compares it with the saved file. */
	heading_line?: string;
}

export function viewingDocumentForThread(threadId: string): ViewingDocument | null {
	const workspace = get(threadWorkspaces)[threadId];
	// Not the presence rule (docPresence.isViewing): that asks which pane has the user's
	// attention, and typing in the composer always moves it to chat. This asks which
	// document is open in the thread's canvas (DCOL-FR9.1), still true while they type.
	if (!workspace?.visible) return null;
	const tab = getActiveTab(workspace);
	if (tab?.kind !== 'document' || !tab.artifactId) return null;
	const heading = headingReaders.get(tab.instanceId)?.();
	return {
		artifact_id: tab.artifactId,
		...(heading ? { heading_anchor: heading.anchor, heading_line: heading.line } : {}),
	};
}
