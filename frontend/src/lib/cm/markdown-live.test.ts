import { describe, it, expect, afterEach, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import { forceParsing } from '@codemirror/language';
import { GFM } from '@lezer/markdown';

// Mock mermaid so the widget's async render is fast and deterministic in jsdom
// (real mermaid needs a layout engine / getBBox that jsdom lacks).
vi.mock('mermaid', () => ({
	default: {
		initialize: vi.fn(),
		render: vi.fn(async (_id: string, code: string) => ({ svg: `<svg data-len="${code.length}"><g/></svg>` }))
	}
}));

import { markdownLivePreview } from './markdown-live';

const views: EditorView[] = [];

function mount(doc: string, cursor?: number): EditorView {
	const state = EditorState.create({
		doc,
		selection: cursor != null ? { anchor: cursor } : undefined,
		extensions: [markdown({ extensions: GFM }), markdownLivePreview]
	});
	const view = new EditorView({ state, parent: document.body });
	if (!forceParsing(view, view.state.doc.length, 1_000)) throw new Error('markdown parser did not finish');
	views.push(view);
	return view;
}

afterEach(() => {
	while (views.length) views.pop()!.destroy();
});

const TABLE = ['| Feature | Status |', '|---------|--------|', '| Login   | Done   |'].join('\n');

describe('markdown live preview — table rendering', () => {
	it('renders a GFM table as an HTML table (not raw pipe source)', () => {
		const view = mount(`# Doc\n\n${TABLE}\n`);
		const table = view.dom.querySelector('table');
		expect(table).not.toBeNull();
		// header + body cell content is rendered; raw pipe delimiters are gone
		expect(table!.textContent).toContain('Feature');
		expect(table!.textContent).toContain('Login');
		expect(table!.textContent).not.toContain('|');
	});

	it('keeps the table rendered when the cursor is inside a table row', () => {
		// place the cursor inside the "Login" body row
		const cursor = '# Doc\n\n'.length + TABLE.indexOf('Login') + 2;
		const view = mount(`# Doc\n\n${TABLE}\n`, cursor);
		// the row must not drop to raw pipe/dash source on focus
		expect(view.dom.querySelector('table')).not.toBeNull();
		expect(view.dom.querySelector('table')!.textContent).not.toContain('|');
	});

	it('keeps linked document cells as clickable anchors', () => {
		const source = ['| Doc |', '| --- |', '| [Open](/workspace/shared/linked.md) |'].join('\n');
		const view = mount(source);
		expect(view.dom.querySelector<HTMLAnchorElement>('table a')?.getAttribute('href'))
			.toBe('/workspace/shared/linked.md');
	});
});

describe('markdown live preview — mermaid rendering', () => {
	const MERMAID = ['```mermaid', 'stateDiagram-v2', '  [*] --> Idle', '```'].join('\n');

	it('replaces a ```mermaid fence with a diagram widget (not raw source)', async () => {
		const view = mount(`# Doc\n\n${MERMAID}\n`);
		const holder = view.dom.querySelector('.cm-md-mermaid');
		expect(holder).not.toBeNull();
		// the rendered SVG arrives asynchronously
		await vi.waitFor(() => expect(holder!.querySelector('svg')).not.toBeNull());
		// raw mermaid source is gone from the visible text
		expect(view.contentDOM.textContent).not.toContain('stateDiagram-v2');
	});

	it('keeps the diagram rendered when the cursor is inside the fence', () => {
		const cursor = '# Doc\n\n'.length + MERMAID.indexOf('Idle');
		const view = mount(`# Doc\n\n${MERMAID}\n`, cursor);
		expect(view.dom.querySelector('.cm-md-mermaid')).not.toBeNull();
	});

	it('leaves a non-mermaid fenced block as code (no diagram widget)', () => {
		const js = ['```js', 'const a = 1;', '```'].join('\n');
		const view = mount(`# Doc\n\n${js}\n`);
		expect(view.dom.querySelector('.cm-md-mermaid')).toBeNull();
		expect(view.contentDOM.textContent).toContain('const a = 1;');
	});
});

describe('markdown live preview — marks never reveal raw source on focus', () => {
	it('hides a heading mark even when the cursor is on that line', () => {
		// cursor right after "# " on the heading line
		const view = mount('# Heading one\n\nbody\n', 2);
		// the rendered heading text must not include the raw "#" mark
		const text = view.contentDOM.textContent ?? '';
		expect(text).toContain('Heading one');
		expect(text).not.toContain('# Heading one');
	});

	it('hides emphasis marks even when the cursor is inside the emphasized span', () => {
		const doc = 'A line with **bold** text.\n';
		const view = mount(doc, doc.indexOf('bold') + 1); // cursor inside "bold"
		const text = view.contentDOM.textContent ?? '';
		expect(text).toContain('bold');
		expect(text).not.toContain('**bold**');
	});
});
