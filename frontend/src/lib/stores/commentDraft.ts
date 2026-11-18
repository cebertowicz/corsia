import { get, writable } from 'svelte/store';
import type { CommentSelection } from './commentSelection';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import type { createMentionSelection } from '$lib/components/comments/mentionSelection';

/** A name for what a comment is on, or a read of it that waits until a draft is started. */
export type SelectionLabel = string | (() => string | undefined);

export interface CommentDraft {
	from: number;
	to: number;
	text: string;
	top: number;
	/** What the comment is on when the quote is not the name for it: a whole table cell, several cells, a whole table or diagram. */
	label?: string;
	/** The comment is on a table or diagram widget, which paints its mark only once the comment is placed: keep the pending mark until then. */
	holdUntilDrawn?: boolean;
	body?: string;
	mentionSelection?: ReturnType<typeof createMentionSelection>;
	/** Why the last press of Comment did not post; the composer shows it until the next press. */
	error?: string;
	scope?: string;
	selection?: CommentSelection;
	identity?: object;
	request?: AbortController;
	sequence?: number;
}

export const commentDraft = writable<CommentDraft | null>(null);

export function commentDraftScope(target: DocViewerTarget | null): string {
	return target ? `${target.instanceId}|${target.path}|${target.threadId ?? ''}|${target.projectId ?? ''}` : '';
}
export function cancelCommentCapture(scope?: string): void {
	const draft = get(commentDraft);
	if (scope === undefined || draft?.scope === undefined || draft.scope === scope) draft?.request?.abort();
}
/** Cancel: the one way a draft and a post it started are dropped. */
export function discardCommentDraft(scope?: string): void {
	cancelCommentCapture(scope);
	dropCommentDraft(scope);
}
/** The viewer that showed this draft is gone or shows another file: it lets go of the draft, and a post already pressed goes on. Its text stays kept (`keptCommentDrafts`). */
export function dropCommentDraft(scope?: string): void {
	commentDraft.update(draft => scope === undefined || draft?.scope === undefined || draft.scope === scope ? null : draft);
}
export function setCommentBody(body: string, scope?: string): void {
	commentDraft.update(draft => draft && (scope === undefined || draft.scope === scope) ? { ...draft, body } : draft);
}
