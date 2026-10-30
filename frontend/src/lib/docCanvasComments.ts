import { hasNewReplies, rangeFor, type DocComment } from '$lib/stores/docComments';
import type { CommentRange, InlineSuggestion, ScrollOrigin } from '$lib/cm/comment-marks';
import { agentColor } from '$lib/docPresence';

export type CanvasScrollTarget = { from: number; to: number; focus?: boolean; origin: ScrollOrigin; commentId?: string };

/**
 * Where a comment sits in the text on screen. An orphaned card has no place to mark, jump to or sort by.
 * A refused card can still have its quote on a stale screen, which is why this is not `rangeFor` alone.
 */
export function canvasCommentRange(comment: DocComment, text: string): { from: number; to: number } | null {
	return comment.orphaned || unplaced(comment) ? null : rangeFor(comment, text);
}

/** A live row never drawn yet (4.2): its offsets are the server's, so it has no highlight or jump. */
export function unplaced(comment: DocComment): boolean {
	return (comment.placement === 'placing' || comment.placement === 'not_placed') && !comment.drawn;
}

/** Whether a just-posted comment may still get its first drawn mark: it exists, is not orphaned or refused placement, and is not drawn yet. */
export const awaitsFirstMark = (comment: DocComment | undefined, drawn: boolean) =>
	!!comment && !comment.orphaned && comment.placement !== 'not_placed' && !drawn;

/** Build the editor's immutable target from the canonical comment range. */
export function canvasCommentScrollTarget(comments: DocComment[], text: string, id: string, focus: boolean, origin: ScrollOrigin): CanvasScrollTarget | null {
	const comment = comments.find((item) => item.id === id);
	const range = comment ? canvasCommentRange(comment, text) : null;
	return range ? { ...range, commentId: id, focus, origin } : null;
}

/** Preserve a concrete target for the same focus, otherwise build explicit navigation. */
export function canvasFocusedCommentScrollTarget(current: CanvasScrollTarget | null, comments: DocComment[], text: string, id: string, focus: boolean): CanvasScrollTarget | null {
	return current?.commentId === id ? current : canvasCommentScrollTarget(comments, text, id, focus, 'navigation');
}

/** A suggestion is drawn inline only while it is open: a decided, resolved or stale one has no marks. */
const openSuggestion = (comment: DocComment) =>
	comment.replacement != null && !comment.decision && !comment.resolved && !comment.review && !comment.orphaned;

/** The agent's presence colour: the thread that wrote the suggestion, the same source as its caret. */
const suggestionColor = (comment: DocComment) => agentColor(comment.authorThreadId || comment.author);

/**
 * The highlight ranges for the editor. An open suggestion rides on its range (`suggestion`), drawn in place of
 * the anchor highlight. Of suggestions that overlap, the earliest in document order is inline and the rest are
 * card-only. `accepting` is the id whose Accept is in flight.
 */
export function canvasCommentHighlights(comments: DocComment[], text: string, focusedId: string | null, onMarkerFocus: CommentRange['onMarkerFocus'], accepting: string | null = null): CommentRange[] {
	let inlineEnd = -1;
	return comments.filter((comment) => (!comment.resolved || comment.id === focusedId) && !(comment.replacement != null && comment.decision)).flatMap((comment) => {
		const anchorRange = canvasCommentRange(comment, text);
		if (!anchorRange) return [];
		const range = comment.insertAfter && openSuggestion(comment) ? { from: anchorRange.to, to: anchorRange.to } : anchorRange;
		let suggestion: InlineSuggestion | undefined;
		if (openSuggestion(comment)) {
			const inline = range.from >= inlineEnd;
			if (inline) inlineEnd = range.to;
			suggestion = { quote: comment.insertAfter ? '' : comment.anchor, insert: comment.replacement!, color: suggestionColor(comment), inline, busy: comment.id === accepting };
		}
		return [{ ...range, id: comment.id, focused: comment.id === focusedId, replyWaiting: hasNewReplies(comment), onMarkerFocus, suggestion }];
	});
}

/**
 * Shared comment state for the document surface: the header receives open-thread and orphaned counts; the drawer's
 * placement recovery also receives the live reload state. The orphaned count is the list's 'Text changed' group, so a
 * resolved thread is never in it: a decided suggestion is resolved, and Accept replaced its quote by design. The
 * recovery's count is the part of that group Place again can attempt: an orphaned unresolved comment whose quote
 * remains in the text.
 */
export function commentSurfaceStatus(comments: DocComment[]) {
	return {
		commentCount: comments.filter((comment) => !comment.resolved).length,
		orphanedCount: comments.filter((comment) => comment.orphaned && !comment.resolved).length,
		replaceableCount: comments.filter((comment) => comment.orphaned && !comment.resolved && !comment.detached).length,
		placementReloading: comments.some((comment) => comment.placementReloading),
		placementReloadError: comments.some((comment) => comment.placementReloadError),
	};
}

/**
 * Use canonical resolved anchors, including resolved threads whose text survives.
 * The only place a listed comment's `orphaned` is set. The text on screen decides it, as before;
 * the server's own `orphaned` from a load is not an input. The one addition is a suggestion whose
 * Accept the server refused (`refused`, from `suggestionRefusals`): that card shows as orphaned
 * until its refusal is cleared, even where a stale screen still shows its quote.
 * A live row not yet placed (4.2) is never orphaned; it sorts by the server's offset.
 * Orphaned cards sort after located ones. Comments on one image read in their tags' number order (FR13.6).
 */
export function commentsInDocumentOrder(comments: DocComment[], text: string, refused: Record<string, string> = {}): DocComment[] {
	const at = (comment: DocComment) => (unplaced(comment) ? comment.from : canvasCommentRange(comment, text)?.from) ?? Infinity;
	const sameImage = (a: DocComment, b: DocComment) => (a.regionNumber != null && b.regionNumber != null && a.imageSrc === b.imageSrc ? a.regionNumber - b.regionNumber : 0);
	return comments.map((comment) => {
		const orphaned = !comment.review && !unplaced(comment) && (comment.id in refused || rangeFor(comment, text) === null);
		const detached = orphaned && (!comment.anchor || !text.includes(comment.anchor));
		return { ...comment, orphaned, detached };
	}).sort((a, b) => at(a) - at(b) || sameImage(a, b) || (a.createdAt ?? '').localeCompare(b.createdAt ?? '') || a.id.localeCompare(b.id));
}
