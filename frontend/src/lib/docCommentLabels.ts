import type { DocComment } from '$lib/stores/docComments';

/** Reads a table comment's label from the editor's own syntax tree: null for none, undefined while the editor is not up. */
export type TableLabelReader = (from: number | undefined, to: number | undefined, quote: string) => string | null | undefined;

/**
 * The card label for a comment on a table's text. It is read once per comment as it loads or moves, never per render, so the
 * tree the label needs is not walked again for each card each time the list updates.
 */
export function createCellLabels(reader: () => TableLabelReader | undefined) {
	const labels = new Map<string, string | null>();
	return (comment: Pick<DocComment, 'id' | 'from' | 'to' | 'anchor' | 'orphaned'>): string | null => {
		const read = reader();
		if (comment.orphaned || !read) return null;
		const key = `${comment.id}|${comment.from}|${comment.to}|${comment.anchor}`;
		if (labels.has(key)) return labels.get(key)!;
		const label = read(comment.from, comment.to, comment.anchor);
		if (label !== undefined) labels.set(key, label);
		return label ?? null;
	};
}
