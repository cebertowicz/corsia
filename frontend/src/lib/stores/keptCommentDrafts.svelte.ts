// The text typed in a new comment's box on a document or an image, with its tags, until the server stores it or the person
// presses Cancel. Closing the box any other way (Escape, a click outside, another card, another tab, a reload) keeps it here,
// and the next box opened on the same document or image starts from it. A post that failed keeps the reason it gave.
// A reply being typed on a document's or an image's thread is kept here the same way (`replyDraftKey`), with its caret, until
// it posts or the person empties its field, so leaving the viewer (another conversation, tab or pane) and coming back finds it.
// It lives as long as the page, as a chat comment's draft does (`chatCommentDrafts`). From the first press of Comment
// it also holds the draft's one client id, which every later press posts under, so the server never stores it twice.
// A box opened from it keeps that id with it (`origin`), so a box left unedited posts under it even once the draft is gone.
import { createMentionSelection } from '$lib/components/comments/mentionSelection';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { newClientId } from './documentWire';

export type MentionSelection = ReturnType<typeof createMentionSelection>;
export type KeptCommentDraft = { body: string; mentionSelection: MentionSelection; error?: string; clientId?: string; selection?: [number, number] };
/** The kept draft a box opened from, once Comment was pressed on it: its id, and the text and tags it held then. */
export type DraftOrigin = { clientId: string; body: string; tags: string };

/** A draft's tagged agents, compared as a set, as the server stores them. */
const tagsOf = (tags: MentionSelection, body: string) => [...new Set(tags.occurrences(body).map((item) => item.agent_id))].sort().join('\n');
const sameTags = (tags: MentionSelection, body: string, sent: string[]) => tagsOf(tags, body) === [...new Set(sent)].sort().join('\n');

/**
 * One reactive cell per key, so a card reads only its own draft: typing in one reply changes nothing another card
 * reads (a map-wide version would re-run every card without a draft on each keystroke). Raw, so the tags object is
 * kept as it is, not proxied.
 */
class Cell { draft = $state.raw<KeptCommentDraft | undefined>(); }
const cells = new Map<string, Cell>();
const cell = (key: string): Cell => {
	let found = cells.get(key);
	if (!found) cells.set(key, found = new Cell());
	return found;
};
const kept = {
	get: (key: string) => cell(key).draft,
	set: (key: string, draft: KeptCommentDraft) => { cell(key).draft = draft; },
	delete: (key: string) => { const found = cells.get(key); if (found) found.draft = undefined; },
};

/** One document, whichever tab or viewer shows it. */
export function documentDraftKey(target: Pick<DocViewerTarget, 'path' | 'projectId'> | null): string {
	return target ? `document:${JSON.stringify([target.projectId ?? '', target.path])}` : '';
}
/** One image: an image artifact, or one image inside a document. */
export function imageDraftKey(artifactId: string | null, image = ''): string {
	return artifactId ? `image:${JSON.stringify([artifactId, image])}` : '';
}
/** A reply to one thread, under the document or image it is written on (`documentDraftKey`, `imageDraftKey`); a thread's id alone keys it where there is neither. */
export function replyDraftKey(scope: string, commentId: string): string {
	return `reply:${JSON.stringify([scope, commentId])}`;
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
	/** A reply's field. A reply has no Cancel: emptying its field clears it, reason and id included. */
	keepReply(key: string, body: string, mentionSelection: MentionSelection): void {
		if (!key) return;
		if (body) kept.set(key, { ...kept.get(key), body, mentionSelection });
		else kept.delete(key);
	},
	/** Where the caret was in a kept field, so the field opens with it there. */
	select(key: string, selection: [number, number] | null): void {
		const prior = key ? kept.get(key) : undefined;
		if (prior) kept.set(key, { ...prior, selection: selection ?? undefined });
	},
	/**
	 * A post failed: the text stays with the reason, which the box shows until the next press of Comment. With
	 * `freshId` (its earlier version is stored under the old id) the next press posts it as a new comment.
	 */
	fail(key: string, body: string, mentionSelection: MentionSelection, error: string, freshId = false): void {
		const prior = key ? kept.get(key) : undefined;
		if (key) kept.set(key, { body, mentionSelection, error, clientId: freshId ? undefined : prior?.clientId, selection: prior?.selection });
	},
	clearError(key: string): void {
		const prior = key ? kept.get(key) : undefined;
		if (prior?.error) kept.set(key, { body: prior.body, mentionSelection: prior.mentionSelection, clientId: prior.clientId, selection: prior.selection });
	},
	/** What a box opening from this draft carries with it: the draft's id once Comment was pressed, with the text and tags it held. */
	origin(key: string): DraftOrigin | undefined {
		const prior = key ? kept.get(key) : undefined;
		return prior?.clientId ? { clientId: prior.clientId, body: prior.body, tags: tagsOf(prior.mentionSelection, prior.body) } : undefined;
	},
	/**
	 * Comment was pressed: the draft's one id, minted on its first press and kept with its text until it posts or is
	 * cancelled. `again` says an earlier press used it, so that press may have stored the comment already. A box opened
	 * from the draft (`origin`) whose post was stored meanwhile, in a viewer since closed, presses under the same id while
	 * its text and tags are as it opened: the server answers with the stored comment. Any edit since posts fresh.
	 */
	claim(key: string, body: string, mentionSelection: MentionSelection, origin?: DraftOrigin): { clientId: string; again: boolean } {
		const prior = key ? kept.get(key) : undefined;
		if (prior?.clientId) return { clientId: prior.clientId, again: true };
		if (!prior && origin && origin.body === body && origin.tags === tagsOf(mentionSelection, body)) {
			// Kept under that id, as a first press keeps its own, so a press after this one failing reuses it.
			if (key) kept.set(key, { body, mentionSelection, clientId: origin.clientId });
			return { clientId: origin.clientId, again: true };
		}
		const clientId = newClientId();
		if (key) kept.set(key, { ...prior, body, mentionSelection, clientId });
		return { clientId, again: false };
	},
	/** Cancel, or the server stored the comment. */
	forget(key: string): void { kept.delete(key); },
	/**
	 * The server stored the draft posted as `sent` with `sentTags`. If it still holds just that, the draft goes and this
	 * says so (true); if its text or tags changed meanwhile, they stay, with their own copy of the tags (the box that
	 * pressed clears its own) and a fresh id for their own post.
	 */
	posted(key: string, sent: string, sentTags: string[]): boolean {
		const prior = key ? kept.get(key) : undefined;
		if (!prior) return false;
		if (prior.body.trim() === sent.trim() && sameTags(prior.mentionSelection, prior.body, sentTags)) { kept.delete(key); return true; }
		kept.set(key, { body: prior.body, mentionSelection: prior.mentionSelection.copy(), selection: prior.selection });
		return false;
	},
	/** Tests only: a fresh page. */
	reset(): void { for (const found of cells.values()) found.draft = undefined; },
};
