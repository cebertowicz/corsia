import type { DocViewerTarget } from '$lib/docViewerTarget';
import { recordTabDocumentHeader, tabViewState, type DocumentHeader } from '$lib/stores/tabViewState';

/**
 * The header row a tab last showed, drawn in the first frame of its return so the row is not missing until the load
 * answers. A stand-in only: nothing but the first frame reads it, it is inert until the load has answered, and what the
 * load answers (a row that is now gone included) replaces it.
 */
export function rememberedHeader(target: DocViewerTarget): DocumentHeader | null {
	const kept = target.threadId ? tabViewState(target.threadId, target.instanceId)?.documentHeader : null;
	return kept && (kept.freshness || kept.ownership) ? kept : null;
}

/** Keeps what the load, a picked owner or supervisor, or a verification last said for this tab's next return. */
export function rememberHeader(target: DocViewerTarget | null, header: DocumentHeader): void {
	if (target?.threadId) recordTabDocumentHeader(target.threadId, target.instanceId, header);
}
