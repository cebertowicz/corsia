import type { DocViewerTarget } from '$lib/docViewerTarget';
import { closeLiveDocument } from './liveDocuments';
import { clearDraft, flushDraft } from './docSaveQueue';

export interface CloseGateCallbacks {
	onClose: () => void;
	onBringIntoView: () => void;
}

/** Drain the tab's save before closing; retain and reveal failed edits. */
export async function closeTabWithSaveGate(
	target: Pick<DocViewerTarget, 'instanceId'>,
	callbacks: CloseGateCallbacks,
): Promise<{ closed: boolean }> {
	const ok = await flushDraft(target);
	if (!ok) {
		callbacks.onBringIntoView();
		return { closed: false };
	}
	clearDraft(target);
	// The close sync and doc_close go now; closing never waits on their ack.
	closeLiveDocument(target.instanceId);
	callbacks.onClose();
	return { closed: true };
}
