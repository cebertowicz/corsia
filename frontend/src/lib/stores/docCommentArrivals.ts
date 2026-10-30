import { get } from 'svelte/store';
import { fetchComments, type CommentFeedback } from '$lib/api/docs';
import { docComments, fromServer, loadComments, reshow, type DocComment } from './docComments';
import { endHold, heldIds, holdCall, startHold } from './docCommentHolds';

export interface PreparedCommentArrival {
	comment: DocComment | null;
	feedback: CommentFeedback;
}

export async function prepareCommentArrival(
	artifactId: string,
	commentId: string | null,
): Promise<PreparedCommentArrival> {
	const { comments, feedback } = await fetchComments(artifactId);
	const row = commentId ? comments.find((comment) => comment.id === commentId) : null;
	return { comment: row ? fromServer(row) : null, feedback };
}

/** The rows with this comment added at the top, or in place of its current copy when `replace`. */
const merged = (comment: DocComment, replace: boolean) => (rows: DocComment[]): DocComment[] => {
	const at = rows.findIndex((row) => row.id === comment.id);
	if (at < 0) return [comment, ...rows];
	return replace ? rows.map((row, index) => (index === at ? comment : row)) : rows;
};

export function revealCommentArrival(artifactId: string, arrival: PreparedCommentArrival): void {
	const comment = arrival.comment;
	if (!comment) return;
	endHold(artifactId, comment.id);
	reshow(artifactId, merged(comment, true));
}

/** Keep this comment out of the document's comments until it is released; a copy already shown is set aside. */
export function holdCommentArrival(artifactId: string, commentId: string): void {
	const shown = (get(docComments)[artifactId] ?? []).find((row) => row.id === commentId);
	startHold(artifactId, commentId, shown);
	if (shown) reshow(artifactId);
}

/** Note which agent call a held comment came from, so that call's streamed preview outlasts the hold. */
export const holdCommentCall = holdCall;

/** Show a held comment normally again (every exit but its reveal). Safe to call when it is not held. */
export function releaseCommentArrival(artifactId: string, commentId: string): void {
	const row = endHold(artifactId, commentId);
	if (row) reshow(artifactId, merged(row, false));
	else if (row === null && !(get(docComments)[artifactId] ?? []).some((existing) => existing.id === commentId)) {
		void loadComments(artifactId).catch(() => undefined);
	}
}

export function releaseCommentArrivals(artifactId: string): void {
	for (const commentId of heldIds(artifactId)) releaseCommentArrival(artifactId, commentId);
}
