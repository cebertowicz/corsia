import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import { GFM } from '@lezer/markdown';
import { scrollToWidget } from './widget-scroll';

const TABLE = '| Item | Owner |\n|---|---|\n| Alpha | Dana |\n| Bravo | Eli |';
const DOC = `# Title\n\nIntro paragraph.\n\n${TABLE}\n\nAfter.`;
const TABLE_FROM = DOC.indexOf('| Item');
const BAND = { top: 100, bottom: 600 };

const rect = (top: number, height: number) => ({ top, height, bottom: top + height, left: 0, right: 100, width: 100, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;

/** A real editor holding the table's source, with a drawn table widget (kept out of CodeMirror's own DOM sync) whose rows sit at the given tops. */
function editorWithTable(rowTops: number[] | null) {
	const view = new EditorView({ state: EditorState.create({ doc: DOC, extensions: [markdown({ extensions: GFM })] }), parent: document.body });
	if (rowTops) {
		const widget = document.createElement('div');
		widget.className = 'cm-md-table';
		widget.dataset.srcLength = String(TABLE.length);
		const table = widget.appendChild(document.createElement('table'));
		rowTops.forEach((top, index) => {
			const row = table.insertRow(index);
			row.insertCell(0).textContent = 'cell';
			row.getBoundingClientRect = () => rect(top, 40);
		});
		const drawn = document.createElement('div');
		drawn.appendChild(widget);
		Object.defineProperty(view, 'contentDOM', { value: drawn });
		view.posAtDOM = () => TABLE_FROM;
	}
	const dispatch = vi.spyOn(view, 'dispatch');
	return { view, dispatch };
}

describe('scrollToWidget', () => {
	// jsdom has no layout: CodeMirror's own measure needs range geometry, and the scroll loop needs a controllable frame clock.
	const rangeLayout = Range.prototype as Partial<Pick<Range, 'getClientRects' | 'getBoundingClientRect'>>;
	beforeEach(() => {
		vi.useFakeTimers({ toFake: ['requestAnimationFrame'] });
		rangeLayout.getClientRects = () => [] as unknown as DOMRectList;
		rangeLayout.getBoundingClientRect = () => rect(0, 0);
	});
	afterEach(() => {
		vi.useRealTimers();
		delete rangeLayout.getClientRects;
		delete rangeLayout.getBoundingClientRect;
		document.body.innerHTML = '';
	});

	it('never retargets CodeMirror at the table block when the row is already drawn and in view', () => {
		// Posting a comment on a visible row re-focuses it. CodeMirror would centre the whole replaced block, jump the scroller
		// to the block's middle and, in the measure that follows, remove the lines above it from the document.
		const { view, dispatch } = editorWithTable([300, 340]);
		const pos = DOC.indexOf('Dana');
		view.scrollDOM.scrollTop = 0;
		expect(scrollToWidget(view, pos, () => BAND, false)).toBe(true);
		vi.advanceTimersByTime(300);
		expect(dispatch).not.toHaveBeenCalled();
		expect(view.scrollDOM.scrollTop).toBe(0);
	});

	it('moves a drawn row that is out of the visible band with the scroller, not a CodeMirror scroll target', () => {
		const { view, dispatch } = editorWithTable([900, 940]);
		view.scrollDOM.scrollTop = 0;
		scrollToWidget(view, DOC.indexOf('Dana'), () => BAND, false);
		vi.advanceTimersByTime(50);
		expect(dispatch).not.toHaveBeenCalled();
		expect(view.scrollDOM.scrollTop).toBeGreaterThan(0);
	});

	it('still asks CodeMirror to bring a table that is not drawn on screen', () => {
		const { view, dispatch } = editorWithTable(null);
		expect(scrollToWidget(view, DOC.indexOf('Dana'), () => BAND, false)).toBe(true);
		expect(dispatch).toHaveBeenCalledTimes(1);
	});

	it('leaves a position outside any widget to the caller', () => {
		const { view, dispatch } = editorWithTable([300, 340]);
		expect(scrollToWidget(view, DOC.indexOf('Intro'), () => BAND, false)).toBe(false);
		expect(dispatch).not.toHaveBeenCalled();
	});
});
