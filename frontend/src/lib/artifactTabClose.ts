import { DocFeedbackError, sendDocFeedback } from '$lib/api/docFeedback';
import { returnToPlanNode } from '$lib/planLeafReturn';
import { closeArtifactTab, setActiveSurface } from '$lib/stores/artifactWorkspace';
import { closeTabWithSaveGate } from '$lib/stores/docSaveQueue';
import { toasts } from '$lib/stores/toast';
import type { ArtifactTab } from '$lib/artifactTabTypes';

/**
 * The one explicit-close path for an artifact tab, shared by the tab strip and
 * the compact tab sheet. A document tab first flushes its pending save (a
 * failed save brings the tab into view instead of closing it), then sends the
 * user's open comment batch at once (DCOL-FR4.2) before the tab goes.
 */
export async function requestArtifactTabClose(
	threadId: string,
	tab: ArtifactTab,
	onBringIntoView: () => void,
): Promise<void> {
	if (tab.kind !== 'document') {
		closeArtifactTab(threadId, tab.instanceId);
		if (tab.kind === 'subagent') {
			setActiveSurface(threadId, 'chat');
			void returnToPlanNode(tab);
		}
		return;
	}
	const result = await closeTabWithSaveGate({ instanceId: tab.instanceId }, {
		onClose: () => {},
		onBringIntoView,
	});
	if (!result.closed) return;
	if (tab.artifactId) {
		// A failed send never traps the tab: the comments stay in the open
		// batch and the Send control returns when the document is reopened.
		// A 404 means the artifact is gone, so there is nothing to send.
		try {
			await sendDocFeedback(tab.artifactId);
		} catch (error) {
			if (!(error instanceof DocFeedbackError && error.status === 404)) {
				toasts.show("Couldn't send your comments. Reopen the document to send them.", 'error');
			}
		}
	}
	closeArtifactTab(threadId, tab.instanceId);
}
