import { get, writable } from 'svelte/store';
import type { CommentSelection } from './commentSelection';
import type { DocViewerTarget } from '$lib/docViewerTarget';

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
export function discardCommentDraft(scope?: string): void {
	cancelCommentCapture(scope);
	commentDraft.update(draft => scope === undefined || draft?.scope === undefined || draft.scope === scope ? null : draft);
}
export function setCommentBody(body: string, scope?: string): void {
	commentDraft.update(draft => draft && (scope === undefined || draft.scope === scope) ? { ...draft, body } : draft);
}
