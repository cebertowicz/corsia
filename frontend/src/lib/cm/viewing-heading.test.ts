import { describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { markdown } from '@codemirror/lang-markdown';
import { nearestHeadingSourceLine, viewingHeading } from './viewing-heading';

describe('nearest heading above the document viewport', () => {
	const text = 'Intro\n\n# One\n\nparagraph\n\n```md\n# False heading\n```\n\nTwo\n---\n\nbody\n\n## One\nlast';
	const state = EditorState.create({ doc: text, extensions: [markdown()] });
	it('uses ATX and Setext source lines, including repeated headings, and excludes fenced code', () => {
		expect(nearestHeadingSourceLine(state, 0)).toBeNull();
		expect(nearestHeadingSourceLine(state, text.indexOf('# One'))).toBe(3);
		expect(nearestHeadingSourceLine(state, text.indexOf('# False'))).toBe(3);
		expect(nearestHeadingSourceLine(state, text.indexOf('body'))).toBe(11);
		expect(nearestHeadingSourceLine(state, text.indexOf('last'))).toBe(16);
	});
	it('includes a heading at the very top of the document', () => {
		const atTop = EditorState.create({ doc: '# First\nbody', extensions: [markdown()] });
		expect(nearestHeadingSourceLine(atTop, 0)).toBe(1);
	});
	it('locates CodeMirror from the open document DOM and reads its current scroll position', () => {
		const root = document.createElement('div');
		root.innerHTML = '<div class="cm-editor"></div>';
		const block = vi.fn(() => ({ from: text.indexOf('body') }));
		const lookup = vi.spyOn(EditorView, 'findFromDOM').mockReturnValue({
			state, scrollDOM: { scrollTop: 240 }, lineBlockAtHeight: block,
		} as unknown as EditorView);
		expect(viewingHeading(root)).toEqual({ anchor: 11, line: 'Two' });
		expect(block).toHaveBeenCalledWith(240);
		expect(lookup).toHaveBeenCalledWith(root.querySelector('.cm-editor'));
		lookup.mockRestore();
	});
	it('sends a heading line whole or not at all', () => {
		const root = document.createElement('div');
		root.innerHTML = '<div class="cm-editor"></div>';
		const read = (heading: string) => {
			const long = EditorState.create({ doc: `${heading}\nbody`, extensions: [markdown()] });
			const lookup = vi.spyOn(EditorView, 'findFromDOM').mockReturnValue({
				state: long, scrollDOM: { scrollTop: 0 }, lineBlockAtHeight: () => ({ from: heading.length + 1 }),
			} as unknown as EditorView);
			const result = viewingHeading(root);
			lookup.mockRestore();
			return result;
		};
		const atLimit = `# ${'a'.repeat(198)}`;
		expect(read(atLimit)).toEqual({ anchor: 1, line: atLimit });
		// One character past what the server compares: none is sent.
		expect(read(`${atLimit}a`)).toBeNull();
	});
	it('omits the heading when the document/editor is not mounted', () => {
		expect(viewingHeading(undefined)).toBeNull();
		expect(viewingHeading(document.createElement('div'))).toBeNull();
	});
});
