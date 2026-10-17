import { describe, expect, it, vi } from 'vitest';
import { createCellLabels, type TableLabelReader } from './docCommentLabels';

const comment = { id: 'a', from: 10, to: 14, anchor: 'Dana', orphaned: false };

describe('createCellLabels', () => {
	it("reads a comment's label once however often the list renders", () => {
		const read = vi.fn<TableLabelReader>(() => '“Dana” · Alpha, Owner');
		const label = createCellLabels(() => read);
		expect([label(comment), label(comment), label(comment)]).toEqual(Array(3).fill('“Dana” · Alpha, Owner'));
		expect(read).toHaveBeenCalledTimes(1);
		expect(read).toHaveBeenCalledWith(10, 14, 'Dana');
	});

	it('reads again for a comment that moved, and keeps a "no label" answer', () => {
		const read = vi.fn<TableLabelReader>(() => null);
		const label = createCellLabels(() => read);
		expect(label(comment)).toBeNull();
		expect(label(comment)).toBeNull();
		expect(read).toHaveBeenCalledTimes(1);
		label({ ...comment, from: 30, to: 34 });
		expect(read).toHaveBeenCalledTimes(2);
	});

	it('does not remember an answer given before the editor was up', () => {
		let reader: TableLabelReader | undefined;
		const label = createCellLabels(() => reader);
		expect(label(comment)).toBeNull();
		reader = () => undefined;
		expect(label(comment)).toBeNull();
		reader = () => 'The whole table';
		expect(label(comment)).toBe('The whole table');
	});

	it('gives an orphaned comment no label and does not read for it', () => {
		const read = vi.fn<TableLabelReader>(() => 'x');
		expect(createCellLabels(() => read)({ ...comment, orphaned: true })).toBeNull();
		expect(read).not.toHaveBeenCalled();
	});
});
