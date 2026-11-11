import { writable } from 'svelte/store';
import type { EmailDraftInfo } from '$lib/emailDraft';

/**
 * The newest email-draft reading the server sent for each loaded document, by artifact id: from the document's load, then
 * from each live frame that carries one. Only the draft record makes a file an email draft, so a document with no entry
 * here opens as an ordinary document.
 */
export const emailDraftHeaders = writable<Record<string, EmailDraftInfo>>({});

/** A load says whether the file is an email draft; a load that says it isn't clears what an earlier one said. */
export function noteDocumentLoad(artifactId: string | null, draft: EmailDraftInfo | null | undefined): void {
	if (!artifactId) return;
	emailDraftHeaders.update((all) => {
		if (draft) return { ...all, [artifactId]: draft };
		if (!(artifactId in all)) return all;
		const rest = { ...all };
		delete rest[artifactId];
		return rest;
	});
}

/** A live frame's newer reading of the header, for a document its load found to be an email draft. */
export function noteLiveHeader(artifactId: string | undefined, draft: EmailDraftInfo | undefined): void {
	if (!artifactId || !draft) return;
	emailDraftHeaders.update((all) => ({ ...all, [artifactId]: draft }));
}
