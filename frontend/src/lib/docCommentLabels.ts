import type { DocComment } from '$lib/stores/docComments';

/**
 * Reads a table comment's label from the editor's own syntax tree: null for none, undefined while the editor is not up.
 * `stamp` names the text the answer was read from and changes with every edit of it, because a label depends on its whole table.
 */
export interface WidgetLabelReader {
	read(from: number | undefined, to: number | undefined, quote: string): string | null | undefined;
	stamp(): unknown;
}

/**
 * The card label for a comment on a table's text. It is read once per comment as it loads, moves or the text it sits in is edited
 * (its table's extent, empty cells, header and first column all shape it), never per render, so the tree the label needs is not
 * walked again for each card each time the list updates.
 */
export function createCellLabels(reader: () => WidgetLabelReader | undefined) {
	// One entry per comment, replaced when its offsets, quote or text change, so it is bounded by the comments themselves.
	const labels = new Map<string, Pick<DocComment, 'from' | 'to' | 'anchor'> & { stamp: unknown; label: string | null }>();
	return (comment: Pick<DocComment, 'id' | 'from' | 'to' | 'anchor' | 'orphaned'>): string | null => {
		const read = reader();
		if (comment.orphaned || !read) return null;
		const cached = labels.get(comment.id), stamp = read.stamp();
		if (cached && cached.stamp === stamp && cached.from === comment.from && cached.to === comment.to && cached.anchor === comment.anchor) return cached.label;
		const label = read.read(comment.from, comment.to, comment.anchor);
		if (label !== undefined) labels.set(comment.id, { stamp, from: comment.from, to: comment.to, anchor: comment.anchor, label });
		return label ?? null;
	};
}
