import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

import { docTheme } from './theme';

function mountedCss(): string {
	const sheets = [...document.styleSheets, ...(document.adoptedStyleSheets ?? [])];
	const rules = sheets.flatMap((sheet) => [...sheet.cssRules].map((rule) => rule.cssText));
	return [...rules, ...[...document.querySelectorAll('style')].map((style) => style.textContent ?? '')].join('\n');
}

describe('document editor theme', () => {
	it('turns transitions off inside the editor under reduced motion', () => {
		// CodeMirror measures in the task it writes in; a transition on every
		// property, however short, leaves the old geometry in the layout.
		const view = new EditorView({ state: EditorState.create({ doc: 'body', extensions: [docTheme] }), parent: document.body });
		const css = mountedCss().replace(/\s+/g, ' ');
		view.destroy();
		expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\) ?\{[^@]*\.cm-scroller, [^{]*\.cm-scroller \* ?\{ ?transition-property: none !important;? ?\}/);
	});
});
