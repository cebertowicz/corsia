import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Rig } from './typed-reveal-rig';
import { caretToWidget } from './widget-caret';
import { build, cellEl, cleanup, clicked, mountedEditor, THREEROW } from './widget-cell-rig';

/**
 * A tap's click reaches the table twice: the cell editor opens the cell, and the widget's own handler would put the document's
 * caret in the pressed cell. With a cell open its editor holds the caret and the focus, so that second write must not happen.
 */

const rigs: Rig[] = [];
afterEach(() => { while (rigs.length) rigs.pop()!.view.destroy(); cleanup(); document.body.innerHTML = ''; });

const pressOn = (target: Element) => Object.defineProperty(new MouseEvent('click'), 'target', { value: target });

describe('caretToWidget', () => {
	it('puts the document caret in the pressed cell when no cell is open', () => {
		const r = build(THREEROW);
		rigs.push(r);
		expect(mountedEditor(r), 'setup: no cell is open').toBeNull();
		const dispatch = vi.spyOn(r.view, 'dispatch');
		caretToWidget(r.view, pressOn(cellEl(r, 1, 1)));
		expect(dispatch.mock.calls.length, 'the caret was written to the document').toBeGreaterThan(0);
	});

	it('writes nothing to the document while a cell is open, so its editor keeps the focus', () => {
		const r = build(THREEROW);
		rigs.push(r);
		clicked(r, cellEl(r, 1, 1));
		expect(mountedEditor(r), 'setup: the cell is open').not.toBeNull();
		const dispatch = vi.spyOn(r.view, 'dispatch');
		caretToWidget(r.view, pressOn(cellEl(r, 1, 0)));
		expect(dispatch).not.toHaveBeenCalled();
	});
});
