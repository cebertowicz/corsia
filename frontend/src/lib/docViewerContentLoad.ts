import { focusedDocComment } from '$lib/stores/docCommentFocus';
import { discardCommentDraft, commentDraftScope } from '$lib/stores/commentDraft';
import { existingDraft, withDraftArtifactId } from '$lib/stores/docSaveQueue';
import { handleViewerFailure } from '$lib/docViewerFailure';
import { loadSignal, loadViewerContent, openFromDraft, type ViewerContent } from '$lib/docViewerLoading';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { DocViewerProps } from '$lib/docViewerProps';
import type { DocOpenRefusal } from '$lib/docOpenRefusal';

/** Starts the existing viewer load, keeping tab drafts authoritative over an older server snapshot. */
export function loadCurrentViewer(context: {
	target: DocViewerTarget;
	previous: DocViewerTarget | null;
	isCurrent: () => boolean;
	onSignal: DocViewerProps['onSignal'];
	begin: (artifactId: string | null, opened: ReturnType<typeof openFromDraft>) => void;
	ready: (content: ViewerContent) => void;
	snapshot: (text: string) => void;
	failure: (failure: { refusal: DocOpenRefusal | null; state: 'ready' | 'error'; text: string | null; readonly: boolean }) => void;
	reloadAttention: () => void;
}) {
	const { target } = context;
	const signal = loadSignal(context.onSignal, target);
	focusedDocComment.set(null);
	discardCommentDraft(commentDraftScope(context.previous));
	const artifactId = withDraftArtifactId(target).artifactId;
	const opened = openFromDraft(existingDraft(target), artifactId);
	context.begin(artifactId, opened);
	signal(opened.signal);
	void loadViewerContent({ target, isCurrent: context.isCurrent, onReady: context.ready,
		onSnapshot: context.snapshot, onUnavailable: () => signal({ status: 'unavailable' }) })
		.catch((error) => {
			if (!context.isCurrent()) return;
			handleViewerFailure(error, opened, { signal, reloadAttention: context.reloadAttention, fail: context.failure });
		});
}
