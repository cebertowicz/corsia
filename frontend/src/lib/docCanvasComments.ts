import { hasUnread, rangeFor, type DocComment } from '$lib/stores/docComments';
import type { CommentRange, InlineSuggestion, ScrollOrigin } from '$lib/cm/comment-marks';
import { agentColor } from '$lib/docPresence';

export type CanvasScrollTarget = { from: number; to: number; focus?: boolean; origin: ScrollOrigin; commentId?: string };

/**
 * Where a comment sits in the text on screen. An orphaned card has no place to mark, jump to or sort by.
 * A refused card can still have its quote on a stale screen, which is why this is not `rangeFor` alone.
 */
export function canvasCommentRange(comment: DocComment, text: string): { from: number; to: number } | null {
	if (comment.orphaned || unplaced(comment)) return null;
	if (acceptedDeletion(comment, text)) return { from: comment.from!, to: comment.from! };
	// A suggestion edited inside keeps its place: its range, not its quote, says where it is.
	if (comment.textChanged && comment.from != null && comment.to != null && comment.to > comment.from && comment.to <= text.length) return { from: comment.from, to: comment.to };
	return rangeFor(comment, text);
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

/** Preserve an explicit target; image-region focus belongs to the image viewer, while text focus navigates the document. */
export function canvasFocusedCommentScrollTarget(current: CanvasScrollTarget | null, comments: DocComment[], text: string, id: string, focus: boolean): CanvasScrollTarget | null {
	if (current?.commentId === id) return current;
	if (comments.find((comment) => comment.id === id)?.region) return null;
	return canvasCommentScrollTarget(comments, text, id, focus, 'navigation');
}

/** What a suggestion's state is read from, on a canvas comment or a card's thread data. */
type SuggestionState = { replacement?: string | null; decision?: string | null; resolved?: boolean; review?: boolean };

/** A suggestion still waiting to be accepted or rejected. A decided one can stay unresolved, so `!resolved` alone never means this. */
export const pendingSuggestion = (comment: SuggestionState) =>
	comment.replacement != null && !comment.decision && !comment.resolved && !comment.review;

/** A suggestion that was accepted or rejected but stays an open conversation: it has replies, and nobody has resolved it. */
export const decidedOpen = (comment: SuggestionState) =>
	comment.replacement != null && !!comment.decision && !comment.resolved && !comment.review;

/** A suggestion is drawn inline only while it is pending and its text still holds. */
const openSuggestion = (comment: DocComment) => pendingSuggestion(comment) && !comment.orphaned && !comment.textChanged;

/** The agent's presence colour: the thread that wrote the suggestion, the same source as its caret. */
const suggestionColor = (comment: DocComment) => agentColor(comment.authorThreadId || comment.author);

/**
 * The highlight ranges for the editor. An open suggestion rides on its range (`suggestion`), drawn in place of
 * the anchor highlight. Of suggestions that overlap, the earliest in document order is inline and the rest are
 * card-only. `accepting` is the id whose Accept is in flight.
 */
export function canvasCommentHighlights(comments: DocComment[], text: string, focusedId: string | null, onMarkerFocus: CommentRange['onMarkerFocus'], accepting: string | null = null): CommentRange[] {
	let inlineEnd = -1;
	return comments.filter((comment) => !comment.resolved || (comment.id === focusedId && !comment.decision)).flatMap((comment): CommentRange[] => {
		const anchorRange = canvasCommentRange(comment, text);
		// A comment whose original text is gone keeps a dashed margin marker at the line its text last was.
		if (!anchorRange && comment.detached && !comment.resolved && comment.regionNumber == null && comment.from != null) {
			const at = Math.min(Math.max(comment.from, 0), text.length);
			return [{ from: at, to: at, id: comment.id, focused: comment.id === focusedId, replyWaiting: hasUnread(comment), onMarkerFocus, point: true, lost: true }];
		}
		if (!anchorRange) return [];
		const range = comment.insertAfter && openSuggestion(comment) ? { from: anchorRange.to, to: anchorRange.to } : anchorRange;
		let suggestion: InlineSuggestion | undefined;
		if (openSuggestion(comment)) {
			const inline = range.from >= inlineEnd;
			if (inline) inlineEnd = range.to;
			suggestion = { quote: comment.insertAfter ? '' : comment.anchor, insert: comment.replacement!, color: suggestionColor(comment), inline, busy: comment.id === accepting };
		}
		return [{ ...range, id: comment.id, focused: comment.id === focusedId, replyWaiting: hasUnread(comment), onMarkerFocus, suggestion, region: !!comment.region, ...(range.from === range.to && !suggestion ? { point: true } : {}) }];
	});
}

/**
 * Shared comment state for the document surface: the header receives open-thread and orphaned counts; the drawer's
 * placement recovery also receives the live reload state. The orphaned count is the list's 'Text changed' group, so a
 * resolved thread is never in it. The recovery's count is the part of that group Place again can attempt: an orphaned
 * unresolved comment whose quote remains in the text.
 */
export function commentSurfaceStatus(comments: DocComment[]) {
	const lost = (comment: DocComment) => comment.orphaned && !comment.resolved;
	return {
		commentCount: comments.filter((comment) => !comment.resolved || hasUnread(comment)).length,
		orphanedCount: comments.filter(lost).length,
		replaceableCount: comments.filter((comment) => lost(comment) && !comment.detached).length,
		placementReloading: comments.some((comment) => comment.placementReloading),
		placementReloadError: comments.some((comment) => comment.placementReloadError),
	};
}

/**
 * A live-tracked comment keeps its two ends where the text moves them: an edit inside its range keeps the thread on the words now inside
 * (the server does the same, so a reload agrees), and text typed just outside is not part of it. An open suggestion is left out: its quote is
 * the text it replaces, so rewriting it would change what it proposes. A decided suggestion behaves as it always has.
 */
function followsText(comment: DocComment, text: string): comment is DocComment & { from: number; to: number } {
	return !!comment.drawn && !comment.review && comment.replacement == null && comment.from != null && comment.to != null && comment.to > comment.from && comment.to <= text.length;
}

/**
 * An accepted suggestion sits on the text it put in the document, not on the quote it replaced, so it is placed,
 * marked and jumped to like any comment. A suggestion whose Accept is in flight reads as accepted once the
 * document holds its replacement, ahead of the server's answer.
 */
function onAppliedText(comment: DocComment, text: string, accepting: string | null): DocComment {
	if (comment.replacement == null || comment.review || comment.from == null) return comment;
	const anchor = comment.insertAfter ? comment.anchor + comment.replacement : comment.replacement;
	const applied = { ...comment, anchor, to: comment.from + anchor.length };
	if (comment.decision === 'accepted') return applied;
	return comment.id === accepting && !comment.decision && text.startsWith(anchor, comment.from) ? { ...applied, decision: 'accepted' } : comment;
}

/** An accepted suggestion that deleted its text has none left by design: it sits at the point where the text was. */
function acceptedDeletion(comment: DocComment, text: string): boolean {
	return comment.decision === 'accepted' && comment.replacement != null && !comment.review && comment.anchor === '' && comment.from != null && comment.from <= text.length;
}

/**
 * An open suggestion whose range still holds text, but not the text it replaces (someone edited inside it): it stays at its line,
 * its card says so, and Accept is off. The range is the live one the editor tracks, or the server's after a reload (`textChanged`).
 */
function textChangedAfter(comment: DocComment, text: string): boolean {
	return comment.replacement != null && !comment.decision && !comment.review && (!!comment.drawn || !!comment.textChanged)
		&& comment.from != null && comment.to != null && comment.to > comment.from && comment.to <= text.length
		&& text.slice(comment.from, comment.to) !== comment.anchor;
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
export function commentsInDocumentOrder(comments: DocComment[], text: string, refused: Record<string, string> = {}, accepting: string | null = null): DocComment[] {
	const at = (comment: DocComment) => (unplaced(comment) ? comment.from : canvasCommentRange(comment, text)?.from) ?? Infinity;
	const sameImage = (a: DocComment, b: DocComment) => (a.regionNumber != null && b.regionNumber != null && a.imageSrc === b.imageSrc ? a.regionNumber - b.regionNumber : 0);
	return comments.map((listed) => {
		const comment = followsText(listed, text) ? { ...listed, anchor: text.slice(listed.from, listed.to) } : onAppliedText(listed, text, accepting);
		const changed = textChangedAfter(comment, text);
		const orphaned = !comment.review && !unplaced(comment) && !changed && (comment.id in refused || (rangeFor(comment, text) === null && !acceptedDeletion(comment, text)));
		// The original text is gone: the range collapsed (everything in it was deleted), or the quote is nowhere in the document.
		const collapsed = !!comment.drawn && comment.from != null && comment.from === comment.to;
		const detached = orphaned && (collapsed || !comment.anchor || !text.includes(comment.anchor));
		return { ...comment, orphaned, detached, textChanged: changed };
	}).sort((a, b) => at(a) - at(b) || sameImage(a, b) || (a.createdAt ?? '').localeCompare(b.createdAt ?? '') || a.id.localeCompare(b.id));
}
