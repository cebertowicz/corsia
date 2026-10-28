import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { ViewerContent } from '$lib/docViewerLoading';
import { enrichDraftTarget, existingDraft, seedDraft } from '$lib/stores/docSaveQueue';
import { reloadDocumentFeedback } from '$lib/docViewerOperations';
import type { TabLoadSignal } from '$lib/artifactTabs';
import type DocViewerStatus from '$lib/components/DocViewerStatus.svelte';

// Feedback loads share the comment store; each call only decides whether this tab may apply its result.
// The freshness state remains with the viewer; the verifier performs only the API transition.
export function applyDocumentContent(d: DocViewerTarget, content: ViewerContent, seq: number, options: {
	loadSeq: () => number;
	shown: () => boolean;
	apply: (content: ViewerContent, legacyAwaitingSave: boolean) => void;
	signal?: (signal: TabLoadSignal) => void;
	feedback: () => DocViewerStatus | undefined;
}): void {
	if (seq !== options.loadSeq()) return;
	seedDraft(d, content.text, content.revision);
	if (content.artifactId && !d.artifactId) enrichDraftTarget(d, content.artifactId);
	const shown = options.shown();
	options.apply(content, !content.live && Boolean(existingDraft(d)?.editText !== existingDraft(d)?.savedText));
	options.signal?.({ status: 'ready', artifactId: content.artifactId, revision: content.revision, writable: content.writable, shown, instanceId: d.instanceId });
	reloadDocumentFeedback(content.artifactId, seq, options.loadSeq, options.feedback());
}
