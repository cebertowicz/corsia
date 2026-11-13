import { afterEach, describe, expect, it } from 'vitest';
import { closeDocumentCell } from '$lib/docCommentSurfaceState.svelte';
import { build, cellEl, cellText, cleanup, clicked, editingCell, mountedEditor, THREEROW } from './widget-cell-rig';

/**
 * A phone's comment sheet is modal: the cell a tap opened closes under it, with no focus left in it, and closing the sheet
 * opens that cell again with the focus, as prose gets its caret back.
 */

afterEach(() => { cleanup(); document.body.innerHTML = ''; });

describe('the cell under a modal comment sheet', () => {
	it('closes and lets go of the focus, and the restore it answers opens the same cell with the focus', () => {
		const r = build(THREEROW);
		const tapped = cellEl(r, 2, 1);
		clicked(r, tapped);
		expect(mountedEditor(r), 'setup: the tap opened the cell').not.toBeNull();
		const before = r.view.state.doc.toString();

		const reopen = closeDocumentCell(r.view.dom.parentElement!);
		expect(reopen, 'a cell was open').not.toBeNull();
		expect(mountedEditor(r), 'the cell closed').toBeNull();
		expect(document.activeElement?.closest('.cm-md-cell-editor'), 'no focus is left in a cell').toBeFalsy();

		reopen!();
		expect(editingCell(r), 'the same cell opens again').toBe(cellEl(r, 2, 1));
		expect(cellText(r)).toBe('Open');
		expect(mountedEditor(r)!.contains(document.activeElement), 'with the focus').toBe(true);
		expect(r.view.state.doc.toString(), 'the document is as it was').toBe(before);
	});

	it('answers nothing to restore when no cell was open', () => {
		const r = build(THREEROW);
		expect(closeDocumentCell(r.view.dom.parentElement!)).toBeNull();
	});
});
