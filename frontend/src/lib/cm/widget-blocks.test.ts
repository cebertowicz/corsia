import { afterEach, describe, expect, it } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import { GFM } from '@lezer/markdown';
import { tableAt, tableRowAt, widgetSourceAt } from './widget-blocks';

const TABLE = '| Item | Owner |\n|---|---|\n| Alpha | Dana |\n| Bravo | Eli |';
const DOC = `# Title\n\n${TABLE}\n\nAfter | text\n\n\`\`\`mermaid\ngraph TD; A-->B\n\`\`\`\n`;
const TABLE_FROM = DOC.indexOf('| Item');
const extensions = [markdown({ extensions: GFM })];

const rect = (top: number) => ({ top, height: 40, bottom: top + 40, left: 0, right: 100, width: 100, x: 0, y: top, toJSON: () => ({}) }) as DOMRect;

/** A table widget for TABLE: header and two body rows at 100, 140 and 180. The delimiter row is not drawn. */
function drawnTable(view: EditorView) {
	const el = document.createElement('div');
	el.className = 'cm-md-table';
	el.dataset.srcLength = String(TABLE.length);
	const table = el.appendChild(document.createElement('table'));
	[100, 140, 180].forEach((top, index) => {
		const row = table.insertRow(index);
		row.insertCell(0).textContent = 'cell';
		row.getBoundingClientRect = () => rect(top);
	});
	view.posAtDOM = () => TABLE_FROM;
	return el;
}

let view: EditorView;
const open = () => view = new EditorView({ state: EditorState.create({ doc: DOC, extensions }), parent: document.body });
afterEach(() => { view?.destroy(); document.body.replaceChildren(); });

describe('tableRowAt', () => {
	const at = (needle: string) => DOC.indexOf(needle, TABLE_FROM);

	it('names the drawn row a source position sits on, by its index among the drawn rows', () => {
		open();
		const el = drawnTable(view);
		expect(tableRowAt(view, el, at('Item'))).toMatchObject({ index: 0, rect: { top: 100 } });
		expect(tableRowAt(view, el, at('Alpha'))).toMatchObject({ index: 1, rect: { top: 140 } });
		expect(tableRowAt(view, el, at('Eli'))).toMatchObject({ index: 2, rect: { top: 180 } });
	});

	it('puts the delimiter row, which is not drawn, on the header', () => {
		open();
		expect(tableRowAt(view, drawnTable(view), at('---'))?.index).toBe(0);
	});

	it("holds the table's first and last source positions", () => {
		open();
		const el = drawnTable(view);
		expect(tableRowAt(view, el, TABLE_FROM)?.index).toBe(0);
		expect(tableRowAt(view, el, TABLE_FROM + TABLE.length)?.index).toBe(2);
	});

	it('is null outside the table, for anything that is not a table, and when its row is not drawn', () => {
		open();
		const el = drawnTable(view);
		expect(tableRowAt(view, el, DOC.indexOf('Title'))).toBeNull();
		expect(tableRowAt(view, el, TABLE_FROM + TABLE.length + 1)).toBeNull();
		const diagram = document.createElement('div');
		diagram.className = 'cm-md-mermaid';
		expect(tableRowAt(view, diagram, at('Alpha'))).toBeNull();
		el.querySelector('table')!.replaceChildren();
		expect(tableRowAt(view, el, at('Alpha'))).toBeNull();
	});
});

describe('tableAt', () => {
	it('finds the table a range touches, as whole source lines, and not a pipe in prose', () => {
		const state = EditorState.create({ doc: DOC, extensions });
		const alpha = DOC.indexOf('Alpha');
		expect(tableAt(state, alpha, alpha + 5)).toEqual({ from: TABLE_FROM, to: TABLE_FROM + TABLE.length });
		const prose = DOC.indexOf('After');
		expect(tableAt(state, prose, prose + 5)).toBeNull();
	});

	it('reads a table that follows a long unparsed document', () => {
		const long = `${'paragraph\n\n'.repeat(5000)}${TABLE}\n\n${'tail\n\n'.repeat(5000)}`;
		const state = EditorState.create({ doc: long, extensions });
		const from = long.indexOf('Dana');
		expect(tableAt(state, from, from + 4)?.from).toBe(long.indexOf('| Item'));
	});
});

describe('widgetSourceAt', () => {
	it('gives the source a table or a mermaid diagram replaces, and nothing for other code or prose', () => {
		open();
		expect(widgetSourceAt(view, DOC.indexOf('Dana'))).toEqual({ from: TABLE_FROM, to: TABLE_FROM + TABLE.length });
		const fence = DOC.indexOf('```mermaid');
		expect(widgetSourceAt(view, DOC.indexOf('A-->B'))).toEqual({ from: fence, to: DOC.length - 1 });
		expect(widgetSourceAt(view, DOC.indexOf('Title'))).toBeNull();
		const code = new EditorView({ state: EditorState.create({ doc: '```js\nlet a\n```', extensions }), parent: document.body });
		expect(widgetSourceAt(code, 8)).toBeNull();
		code.destroy();
	});

	// The renderer draws a diagram only when the fence's info string, trimmed and lowercased, is exactly mermaid.
	const sourceIn = (doc: string, needle: string) => {
		const editor = new EditorView({ state: EditorState.create({ doc, extensions }), parent: document.body });
		try { return widgetSourceAt(editor, doc.indexOf(needle)); } finally { editor.destroy(); }
	};

	it('takes a tilde or an indented mermaid fence as a diagram, as the renderer does', () => {
		const tilde = 'intro\n\n~~~mermaid\ngraph TD; A-->B\n~~~\n';
		expect(sourceIn(tilde, 'A-->B')).toEqual({ from: tilde.indexOf('~~~mermaid'), to: tilde.length - 1 });
		const indented = 'intro\n\n  ```Mermaid \n  graph TD; A-->B\n  ```\n';
		expect(sourceIn(indented, 'A-->B')).toEqual({ from: indented.indexOf('  ```'), to: indented.length - 1 });
	});

	it('gives no target for a mermaid-js or other code fence, which the renderer leaves as code', () => {
		expect(sourceIn('```mermaid-js\ngraph TD; A-->B\n```\n', 'A-->B')).toBeNull();
		expect(sourceIn('```mermaidish\nx\n```\n', 'x')).toBeNull();
		expect(sourceIn('```\nplain\n```\n', 'plain')).toBeNull();
	});
});
