import type { DocViewerTarget } from '$lib/docViewerTarget';
import { recordTabDocumentHeader, tabViewState, type DocumentHeader } from '$lib/stores/tabViewState';
import type { DocOwnership } from '$lib/api/docOwnership';
import type { DocumentFreshness } from '$lib/docFreshness';

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

/**
 * Where a pick or a verification settles. Taken when it starts, so a tab switch while it runs cannot hand its result to
 * the next document: it goes into the remembered row of the document it started on, and into the row on screen only
 * while that document is still the one shown.
 */
export function headerSettler(
	started: DocViewerTarget | null,
	startedId: string | null,
	shown: () => { target: DocViewerTarget | null; artifactId: string | null },
	show: (patch: Partial<DocumentHeader>) => void,
) {
	const settle = (patch: Partial<DocumentHeader>) => {
		const kept = started ? rememberedHeader(started) : null;
		rememberHeader(started, { freshness: kept?.freshness ?? null, ownership: kept?.ownership ?? null, ...patch });
		const now = shown();
		if (now.target?.instanceId === started?.instanceId && now.artifactId === startedId) show(patch);
	};
	return { ownership: (value: DocOwnership) => settle({ ownership: value }), freshness: (next: DocumentFreshness) => settle({ freshness: next }) };
}
