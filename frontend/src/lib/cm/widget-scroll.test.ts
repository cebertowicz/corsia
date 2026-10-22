import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { forceParsing } from '@codemirror/language';
import { markdown } from '@codemirror/lang-markdown';
import { GFM } from '@lezer/markdown';
import { scrollToRange } from './comment-marks';
import { trackOverlaySheet } from './visible-region';

const TABLE = '| Item | Owner |\n|---|---|\n| Alpha | Dana |\n| Bravo | Eli |';
const DOC = `# Title\n\nIntro paragraph.\n\n${TABLE}\n\nAfter.`;
const TABLE_FROM = DOC.indexOf('| Item');
const DANA = DOC.indexOf('Dana');
const BAND = { top: 100, bottom: 600 };

const rect = (top: number, height: number) => ({ top, height, bottom: top + height, left: 0, right: 100, width: 100, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;

/**
 * A real editor holding the table's source, with a drawn table widget (kept out of CodeMirror's own DOM sync) whose rows sit at
 * the given content tops and move with the scroller. `writes` records whether each scroller write ran inside a measure write.
 */
function editorWithTable(rowTops: number[] | null) {
	const view = new EditorView({ state: EditorState.create({ doc: DOC, extensions: [markdown({ extensions: GFM })] }), parent: document.body });
	// The first parse stops after 20ms of wall time and finishes on a timer these tests never run: under load the table would
	// not be parsed yet, and the comment would read as plain text.
	if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	view.scrollDOM.style.overflowY = 'auto';
	view.scrollDOM.getBoundingClientRect = () => rect(BAND.top, BAND.bottom - BAND.top);
	const writes: boolean[] = [];
	let scrollTop = 0, measuring = false;
	Object.defineProperty(view.scrollDOM, 'scrollTop', { get: () => scrollTop, set: (value: number) => { writes.push(measuring); scrollTop = value; } });
	const requestMeasure = view.requestMeasure.bind(view);
	vi.spyOn(view, 'requestMeasure').mockImplementation((spec) => requestMeasure(spec && {
		...spec, write: (value, current) => { measuring = true; try { spec.write?.(value, current); } finally { measuring = false; } },
	}));
	const rows: HTMLElement[] = [];
	if (rowTops) {
		const widget = document.createElement('div');
		widget.className = 'cm-md-table';
		widget.dataset.srcLength = String(TABLE.length);
		const table = widget.appendChild(document.createElement('table'));
		rowTops.forEach((top, index) => {
			const row = table.insertRow(index);
			row.insertCell(0).textContent = 'cell';
			row.getBoundingClientRect = () => rect(top - scrollTop, 40);
			rows.push(row);
		});
		const drawn = document.createElement('div');
		drawn.appendChild(widget);
		Object.defineProperty(view, 'contentDOM', { value: drawn });
		view.posAtDOM = () => TABLE_FROM;
	}
	const dispatch = vi.spyOn(view, 'dispatch');
	const rowRect = (index: number) => rows[index].getBoundingClientRect();
	return { view, dispatch, writes, rowRect, scrollTop: () => scrollTop };
}

describe('scrollToRange inside a table', () => {
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

	it('leaves a visible row where it is after saving a comment on it, even at the edge of the band', () => {
		// Posting on a visible row re-focuses it; prose already in the band does not scroll, and neither does a row. The row
		// below ends exactly on the band's bottom edge, where a centring rule would move it.
		const { view, dispatch, scrollTop } = editorWithTable([520, 560]);
		scrollToRange(view, DANA, DANA, 'comment-save');
		vi.advanceTimersByTime(300);
		expect(dispatch).not.toHaveBeenCalled();
		expect(scrollTop()).toBe(0);
	});

	it('lands a drawn row from outside the band inside it after a quote tap, with the scroller and not a CodeMirror scroll target', () => {
		const { view, dispatch, rowRect } = editorWithTable([900, 940]);
		scrollToRange(view, DANA, DANA, 'quote-tap');
		vi.advanceTimersByTime(300);
		expect(dispatch).not.toHaveBeenCalled();
		expect(rowRect(1).top).toBeGreaterThanOrEqual(BAND.top);
		expect(rowRect(1).bottom).toBeLessThanOrEqual(BAND.bottom);
	});

	it('centres a drawn row for a navigation jump, and stops checking once it holds', () => {
		const { view, rowRect } = editorWithTable([900, 940]);
		scrollToRange(view, DANA, DANA);
		vi.advanceTimersByTime(300);
		expect(rowRect(1).top + 20).toBeCloseTo((BAND.top + BAND.bottom) / 2, 0);
		expect(vi.getTimerCount()).toBe(0);
	});

	/** The desktop list drawer: it sits beside the editor in a two-column grid, so its top is the editor's top. */
	const sideDrawer = () => {
		const drawer = document.body.appendChild(document.createElement('section'));
		drawer.setAttribute('data-doc-comments-drawer', '');
		drawer.getBoundingClientRect = () => rect(BAND.top, BAND.bottom - BAND.top);
	};

	it('is not narrowed by the drawer beside the editor: a saved visible row stays put and a table row centres', () => {
		sideDrawer();
		const saved = editorWithTable([520, 560]);
		scrollToRange(saved.view, DANA, DANA, 'comment-save');
		vi.advanceTimersByTime(300);
		expect(saved.dispatch).not.toHaveBeenCalled();
		expect(saved.scrollTop()).toBe(0);
		const navigated = editorWithTable([900, 940]);
		scrollToRange(navigated.view, DANA, DANA);
		vi.advanceTimersByTime(300);
		expect(navigated.rowRect(1).top + 20).toBeCloseTo((BAND.top + BAND.bottom) / 2, 0);
	});

	it('moves a row that sits behind the sheet of its surface to above the sheet, and keeps one that is above it', () => {
		const backdrop = document.body.appendChild(document.createElement('div'));
		backdrop.setAttribute('data-adaptive-drawer', '');
		const panel = backdrop.appendChild(document.createElement('div'));
		backdrop.getBoundingClientRect = () => rect(0, BAND.bottom);
		Object.defineProperty(panel, 'offsetHeight', { value: 200 });
		const stop = trackOverlaySheet(document.body, () => panel);
		const behind = editorWithTable([520, 560]);
		scrollToRange(behind.view, DANA, DANA, 'comment-save');
		vi.advanceTimersByTime(300);
		// The sheet rests at 400: the row ends above it.
		expect(behind.rowRect(1).bottom).toBeLessThanOrEqual(BAND.bottom - 200);
		expect(behind.rowRect(1).top).toBeGreaterThanOrEqual(BAND.top);
		const above = editorWithTable([200, 240]);
		scrollToRange(above.view, DANA, DANA, 'comment-save');
		vi.advanceTimersByTime(300);
		expect(above.scrollTop()).toBe(0);
		stop();
	});

	it("writes the scroller only inside CodeMirror's measure cycle", () => {
		const { view, writes } = editorWithTable([900, 940]);
		scrollToRange(view, DANA, DANA, 'quote-tap');
		vi.advanceTimersByTime(300);
		expect(writes.length).toBeGreaterThan(0);
		expect(writes.every(Boolean)).toBe(true);
	});

	it('asks CodeMirror for the start of a table that is not drawn, never for the middle of its block', () => {
		const { view, dispatch } = editorWithTable(null);
		scrollToRange(view, DANA, DANA);
		expect(dispatch).toHaveBeenCalledTimes(1);
		const target = (dispatch.mock.calls[0][0] as { effects: { value: { y: string; range: { head: number } } } }).effects.value;
		expect(target.y).toBe('start');
		expect(target.range.head).toBe(TABLE_FROM);
	});

	it('scrolls a position outside any widget as usual', () => {
		const { view, dispatch } = editorWithTable([300, 340]);
		scrollToRange(view, DOC.indexOf('Intro'), DOC.indexOf('Intro'));
		expect(dispatch).toHaveBeenCalledTimes(1);
		expect((dispatch.mock.calls[0][0] as { effects: { value: { y: string } } }).effects.value.y).toBe('center');
	});
});
