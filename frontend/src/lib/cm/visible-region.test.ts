import { afterEach, describe, expect, it } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { trackOverlaySheet, visibleRegion } from './visible-region';

const rect = (top: number, bottom: number) => ({ top, bottom, height: bottom - top, left: 0, right: 100, width: 100, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;

/** An editor on a document surface whose scroller shows 100 to 700. */
function editor() {
	const surface = document.body.appendChild(document.createElement('div'));
	const view = new EditorView({ state: EditorState.create({ doc: 'text' }), parent: surface });
	view.scrollDOM.style.overflowY = 'auto';
	view.scrollDOM.getBoundingClientRect = () => rect(100, 700);
	return { surface, view };
}

/**
 * An AdaptiveDrawer panel as it is mid-intro: its backdrop ends at `backdropBottom`, the panel is `height` tall and, sliding up
 * from 72px low, is drawn that far below where it rests.
 */
function sheet(backdropBottom: number, height: number) {
	const backdrop = document.body.appendChild(document.createElement('div'));
	backdrop.setAttribute('data-adaptive-drawer', '');
	const panel = backdrop.appendChild(document.createElement('div'));
	let bottom = backdropBottom;
	backdrop.getBoundingClientRect = () => rect(0, bottom);
	Object.defineProperty(panel, 'offsetHeight', { get: () => height });
	panel.getBoundingClientRect = () => rect(bottom - height + 72, bottom + 72);
	return { panel, moveBackdropBottom(next: number) { bottom = next; } };
}

afterEach(() => document.body.replaceChildren());

describe('visibleRegion', () => {
	it('is the clipped box of the editor, and ignores a drawer beside it', () => {
		const { view } = editor();
		// The desktop side-by-side list drawer carries the same data attribute and its top is the editor's top.
		const side = document.body.appendChild(document.createElement('section'));
		side.setAttribute('data-doc-comments-drawer', '');
		side.getBoundingClientRect = () => rect(100, 700);
		expect(visibleRegion(view).band).toEqual({ top: 100, bottom: 700 });
	});

	it('subtracts the sheet of its surface at its resting top, not where its intro has it', () => {
		const { surface, view } = editor();
		const { panel } = sheet(700, 200);
		trackOverlaySheet(surface, () => panel);
		// The panel is drawn 72px low mid-intro (top 572); it rests at 500.
		expect(panel.getBoundingClientRect().top).toBe(572);
		expect(visibleRegion(view).band).toEqual({ top: 100, bottom: 500 });
	});

	it('follows the sheet as the keyboard moves it, and leaves it once it is gone', () => {
		const { surface, view } = editor();
		const { panel, moveBackdropBottom } = sheet(700, 200);
		let shown: HTMLElement | null = panel;
		trackOverlaySheet(surface, () => shown);
		moveBackdropBottom(450);
		expect(visibleRegion(view).band.bottom).toBe(250);
		shown = null;
		expect(visibleRegion(view).band.bottom).toBe(700);
	});

	it('does not count the sheet of another pane, nor one that is not attached to the page', () => {
		const { view } = editor();
		const other = document.body.appendChild(document.createElement('div'));
		const { panel } = sheet(700, 200);
		trackOverlaySheet(other, () => panel);
		expect(visibleRegion(view).band.bottom).toBe(700);
		const { surface, view: mine } = editor();
		trackOverlaySheet(surface, () => document.createElement('div'));
		expect(visibleRegion(mine).band.bottom).toBe(700);
	});

	it('stops counting a sheet once its surface unregisters it', () => {
		const { surface, view } = editor();
		const { panel } = sheet(700, 200);
		const stop = trackOverlaySheet(surface, () => panel);
		expect(visibleRegion(view).band.bottom).toBe(500);
		stop();
		expect(visibleRegion(view).band.bottom).toBe(700);
	});

	it('registers nothing for a surface that is not drawn yet', () => {
		const { view } = editor();
		expect(() => trackOverlaySheet(null, () => sheet(700, 200).panel)()).not.toThrow();
		expect(visibleRegion(view).band.bottom).toBe(700);
	});

	it('names the scrolling pane as the scroller when the editor grows to its content', () => {
		const { surface, view } = editor();
		surface.style.overflowY = 'auto';
		Object.defineProperty(surface, 'scrollHeight', { value: 2000 });
		Object.defineProperty(surface, 'clientHeight', { value: 600 });
		expect(visibleRegion(view).scroller).toBe(surface);
		surface.style.overflowY = 'visible';
		expect(visibleRegion(view).scroller).toBe(view.scrollDOM);
	});
});
