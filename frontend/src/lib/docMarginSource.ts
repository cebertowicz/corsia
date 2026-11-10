import type { Snippet } from 'svelte';

/**
 * What a margin stack needs from the text its cards sit beside, with nothing about that text in it:
 * a document answers with its editor's lines, a chat with its transcript's highlights. `docMarginPlacement`
 * keeps the placement math; this keeps where the anchors are and what moves them.
 */
export type MarginAnchorSource = {
	/** The element whose scroll the cards ride. */
	scroller: () => HTMLElement | null;
	/** The column the cards sit in: its top is their origin. Read from the stack mounted inside it. */
	column: (stack: HTMLElement) => HTMLElement | null;
	/** Where the reader sees the scroller's content, for a column drawn on it: a picked card is brought into it. The scroller's own box unless something floats over its edge. */
	view?: () => { top: number; bottom: number } | null;
	/** The element a card is placed beside: the first line of its comment's anchor, or null when the comment has no line at all. */
	anchor: (id: string) => Element | null;
	/** Nodes whose change moves a line: watched for mutation. */
	moved: () => Element[];
	/** Nodes whose own resize moves a line, besides the scroller, the column and the cards. */
	resized: () => Element[];
};

/** A composer drawn as a card in the margin column: its key among the cards, where the text it is written on is now, and what it draws. */
export type MarginComposer = { id: string; anchor: () => DOMRect | null; body: Snippet };

/**
 * The document's own source: the editor's scroller, its commented lines and its suggestion deletions, the
 * content and rail the decorations are drawn in.
 */
export function codeMarginSource(canvas: () => HTMLElement | null): MarginAnchorSource {
	return {
		scroller: () => canvas()?.querySelector<HTMLElement>('.cm-scroller') ?? null,
		column: (stack) => stack.closest<HTMLElement>('.drawer-comments'),
		anchor: (id) => canvas()?.querySelector(`.cm-comment-line[data-comment-ids*="${id}"], .cm-suggestion-del[data-suggestion-id="${id}"]`) ?? null,
		moved: () => [...(canvas()?.querySelectorAll('.cm-content, .cm-comment-rail') ?? [])],
		resized: () => [...(canvas()?.querySelectorAll('.cm-content') ?? [])],
	};
}
