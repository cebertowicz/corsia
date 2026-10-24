import { docOpenRefusal, ImageFileError, type DocOpenRefusal } from '$lib/docOpenRefusal';
import type { DraftOpen } from '$lib/docViewerLoading';
import type { TabLoadSignal } from '$lib/artifactTabs';

export function handleViewerFailure(error: unknown, opened: DraftOpen, handlers: {
	signal: (signal: Omit<TabLoadSignal, 'instanceId'>) => void;
	reloadAttention: () => void;
	fail: (failure: { refusal: DocOpenRefusal | null; message: string; state: 'ready' | 'error'; text: string | null; readonly: boolean }) => void;
}): void {
	// A tab opened before the backend's type was known: the leaf swaps it to the image viewer.
	if (error instanceof ImageFileError) { handlers.signal({ status: 'loading', renderer: 'image' }); return; }
	// A 404 means the document is gone, which the tab shows as its
	// explicit unavailable state rather than a raw load error.
	// A confirmed missing file is terminal, not retryable. The queue remains server-owned:
	// refresh it so any reply row the server has retired disappears with the unavailable tab.
	if (error instanceof Error && /^404\b/.test(error.message) && /\bno such file\b/i.test(error.message)) {
		handlers.signal({ status: 'unavailable' }); handlers.reloadAttention(); return;
	}
	const refusal = docOpenRefusal(error);
	handlers.fail({ refusal, message: error instanceof Error ? error.message : String(error), state: refusal || opened.unsaved ? 'ready' : 'error', text: opened.text, readonly: Boolean(refusal) });
	// Keep a failed reply-open mounted so its file-specific recovery can
	// render. The tab remains an error until the reader retries; marking
	// it unavailable would remove this surface and strand them in chat.
	handlers.signal({ status: 'error' });
}
