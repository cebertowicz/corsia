import { describe, expect, it, vi } from 'vitest';
import { markdown } from '@codemirror/lang-markdown';
import { EditorState } from '@codemirror/state';
import { GFM } from '@lezer/markdown';
import { createCellLabels, type WidgetLabelReader } from './docCommentLabels';
import { widgetCommentLabel } from './cm/widget-cells';

const comment = { id: 'a', from: 10, to: 14, anchor: 'Dana', orphaned: false };
type Read = WidgetLabelReader['read'];
const reading = (read: Read, stamp: () => unknown = () => 0): WidgetLabelReader => ({ read, stamp });

describe('createCellLabels', () => {
	it("reads a comment's label once however often the list renders", () => {
		const read = vi.fn<Read>(() => '“Dana” · Alpha, Owner');
		const label = createCellLabels(() => reading(read));
		expect([label(comment), label(comment), label(comment)]).toEqual(Array(3).fill('“Dana” · Alpha, Owner'));
		expect(read).toHaveBeenCalledTimes(1);
		expect(read).toHaveBeenCalledWith(10, 14, 'Dana');
	});

	it('reads again for a comment that moved, and keeps a "no label" answer', () => {
		const read = vi.fn<Read>(() => null);
		const label = createCellLabels(() => reading(read));
		expect(label(comment)).toBeNull();
		expect(label(comment)).toBeNull();
		expect(read).toHaveBeenCalledTimes(1);
		label({ ...comment, from: 30, to: 34 });
		expect(read).toHaveBeenCalledTimes(2);
	});

	it('does not remember an answer given before the editor was up', () => {
		let reader: WidgetLabelReader | undefined;
		const label = createCellLabels(() => reader);
		expect(label(comment)).toBeNull();
		reader = reading(() => undefined);
		expect(label(comment)).toBeNull();
		reader = reading(() => 'The whole table');
		expect(label(comment)).toBe('The whole table');
	});

	it('gives an orphaned comment no label and does not read for it', () => {
		const read = vi.fn<Read>(() => 'x');
		expect(createCellLabels(() => reading(read))({ ...comment, orphaned: true })).toBeNull();
		expect(read).not.toHaveBeenCalled();
	});

	it('keeps one entry per comment however often its offsets shift', () => {
		const read = vi.fn<Read>(() => 'label');
		const label = createCellLabels(() => reading(read));
		for (let shift = 0; shift < 50; shift++) label({ ...comment, from: 10 + shift, to: 14 + shift });
		expect(read).toHaveBeenCalledTimes(50);
		// Only the latest position is held: a repeat of it does not read, an earlier one does.
		label({ ...comment, from: 59, to: 63 });
		expect(read).toHaveBeenCalledTimes(50);
		label({ ...comment, from: 10, to: 14 });
		expect(read).toHaveBeenCalledTimes(51);
	});

	it('replaces the entry when the quote changes', () => {
		const read = vi.fn<Read>(() => 'label');
		const label = createCellLabels(() => reading(read));
		label(comment);
		label({ ...comment, anchor: 'Eli' });
		expect(read).toHaveBeenCalledTimes(2);
		expect(read).toHaveBeenLastCalledWith(10, 14, 'Eli');
	});

	it('reads again once the text it was read from is edited, though the comment did not move', () => {
		let doc = 'one';
		const read = vi.fn<Read>(() => 'label');
		const label = createCellLabels(() => reading(read, () => doc));
		label(comment);
		label(comment);
		expect(read).toHaveBeenCalledTimes(1);
		doc = 'two';
		label(comment);
		label(comment);
		expect(read).toHaveBeenCalledTimes(2);
	});

	it("follows its table: a whole-table comment stops saying so once a row is added below it", () => {
		const table = '| Name | Status |\n|---|---|\n| Dana | Open |';
		let state = EditorState.create({ doc: table, extensions: [markdown({ extensions: GFM })] });
		const label = createCellLabels(() => reading((from, to, quote) => widgetCommentLabel(state, from, to, quote), () => state.doc));
		const whole = { id: 'w', from: 0, to: table.length, anchor: table, orphaned: false };
		expect(label(whole)).toBe('The whole table');
		// The row is added after the comment's range, so none of its own offsets move.
		state = state.update({ changes: { from: table.length, insert: '\n| Eli | Done |' } }).state;
		expect(label(whole)).not.toBe('The whole table');
		expect(label(whole)).toBe(widgetCommentLabel(state, whole.from, whole.to, whole.anchor));
	});
});
