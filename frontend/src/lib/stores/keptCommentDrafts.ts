// The text typed in a new comment's box on a document or an image, with its tags, until the server stores it or the person
// presses Cancel. Closing the box any other way (Escape, a click outside, another card, another tab, a reload) keeps it here,
// and the next box opened on the same document or image starts from it. A post that failed keeps the reason it gave.
// It lives as long as the page, as a chat comment's draft does (`chatCommentDrafts`).
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import type { DocViewerTarget } from '$lib/docViewerTarget';

export type MentionSelection = ReturnType<typeof createMentionSelection>;
export type KeptCommentDraft = { body: string; mentionSelection: MentionSelection; error?: string };

const kept = new Map<string, KeptCommentDraft>();

/** One document, whichever tab or viewer shows it. */
export function documentDraftKey(target: Pick<DocViewerTarget, 'path' | 'projectId'> | null): string {
	return target ? `document:${JSON.stringify([target.projectId ?? '', target.path])}` : '';
}
/** One image: an image artifact, or one image inside a document. */
export function imageDraftKey(artifactId: string | null, image = ''): string {
	return artifactId ? `image:${JSON.stringify([artifactId, image])}` : '';
}

export const keptCommentDrafts = {
	get(key: string): KeptCommentDraft | undefined { return key ? kept.get(key) : undefined; },
	/** The box's text and the tags it holds; an empty box with no failure to show keeps nothing. */
	keep(key: string, body: string, mentionSelection: MentionSelection): void {
		if (!key) return;
		const prior = kept.get(key);
		if (body || prior?.error) kept.set(key, { ...prior, body, mentionSelection });
		else kept.delete(key);
	},
	/** A post failed: the text stays with the reason, which the box shows until the next press of Comment. */
	fail(key: string, body: string, mentionSelection: MentionSelection, error: string): void {
		if (key) kept.set(key, { body, mentionSelection, error });
	},
	clearError(key: string): void {
		const prior = key ? kept.get(key) : undefined;
		if (prior?.error) kept.set(key, { body: prior.body, mentionSelection: prior.mentionSelection });
	},
	/** Cancel, or the server stored the comment. */
	forget(key: string): void { kept.delete(key); },
	/** Tests only: a fresh page. */
	reset(): void { kept.clear(); },
};
