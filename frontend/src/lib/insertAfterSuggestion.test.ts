import { expect, it } from 'vitest';
import { canvasCommentHighlights, commentsInDocumentOrder } from './docCanvasComments';
import { fromServer } from './stores/docCommentProjection';
import type { ServerComment } from './api/docCommentTypes';

it('keeps the heading as the live anchor while drawing both insertions after it', () => {
	const base: ServerComment = {
		id: 'one', artifact_id: 'doc', parent_id: null, author: 'agent', author_kind: 'agent', body: '',
		anchor_text: 'Heading', from_off: 0, to_off: 7, review: 0, resolved: 0, acked: 0,
		replacement: ' one', insert_after: 1, created_at: '2026-01-01', updated_at: '2026-01-01'
	};
	const rows = commentsInDocumentOrder([fromServer(base), fromServer({ ...base, id: 'two', replacement: ' two' })], 'Heading\n');
	expect(rows.every((row) => !row.orphaned && row.anchor === 'Heading')).toBe(true);
	const marks = canvasCommentHighlights(rows, 'Heading\n', null, undefined);
	expect(marks).toHaveLength(2);
	for (const mark of marks) expect(mark).toMatchObject({ from: 7, to: 7, suggestion: { quote: '', inline: true } });
});
