/**
 * What a margin stack needs from the text its cards sit beside, with nothing about that text in it:
 * a document answers with its editor's lines, a chat with its transcript's highlights. `docMarginPlacement`
 * keeps the placement math; this keeps where the anchors are, what moves them and where the room the
 * stack reserves below the text is written.
 */
export type MarginAnchorSource = {
	/** The element whose scroll the cards ride. */
	scroller: () => HTMLElement | null;
	/** The clip the cards sit in: its top is their origin, its height their room. Read from the stack mounted inside it. */
	clip: (stack: HTMLElement) => HTMLElement | null;
	/** The element a card is placed beside: the first line of its comment's anchor, or null when the comment has no line at all. */
	anchor: (id: string) => Element | null;
	/** Nodes whose change moves a line: watched for mutation. */
	moved: () => Element[];
	/** Nodes whose own resize moves a line, besides the scroller, the clip and the cards. */
	resized: () => Element[];
	/** Where the room kept below the text lives, and its reading-line hold; null while the surface has no canvas yet. */
	room: () => { el: HTMLElement; property: string } | null;
};

/**
 * The document's own source: the editor's scroller, its commented lines and its suggestion deletions, the
 * content and rail the decorations are drawn in, and the room below the text the canvas keeps in `--margin-overhang`.
 */
export function codeMarginSource(canvas: () => HTMLElement | null): MarginAnchorSource {
	return {
		scroller: () => canvas()?.querySelector<HTMLElement>('.cm-scroller') ?? null,
		clip: (stack) => stack.closest<HTMLElement>('.drawer-comments'),
		anchor: (id) => canvas()?.querySelector(`.cm-comment-line[data-comment-ids*="${id}"], .cm-suggestion-del[data-suggestion-id="${id}"]`) ?? null,
		moved: () => [...(canvas()?.querySelectorAll('.cm-content, .cm-comment-rail') ?? [])],
		resized: () => [...(canvas()?.querySelectorAll('.cm-content') ?? [])],
		room: () => {
			const el = canvas();
			return el ? { el, property: '--margin-overhang' } : null;
		},
	};
}
