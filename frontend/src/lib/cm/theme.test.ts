import { describe, it, expect } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';

import { docScrollMargin, docTheme } from './theme';
import { PORTRAIT_CAP_PX, PORTRAIT_CAP_SVH } from './markdown-image-portrait';

function mountedCss(): string {
	const sheets = [...document.styleSheets, ...(document.adoptedStyleSheets ?? [])];
	const rules = sheets.flatMap((sheet) => [...sheet.cssRules].map((rule) => rule.cssText));
	return [...rules, ...[...document.querySelectorAll('style')].map((style) => style.textContent ?? '')].join('\n');
}

describe('document editor theme', () => {
	it('holds a portrait image to the height the two shared cap numbers give, and no other image', () => {
		const view = new EditorView({ state: EditorState.create({ doc: 'body', extensions: [docTheme] }), parent: document.body });
		const css = mountedCss().replace(/\s+/g, ' ');
		view.destroy();
		expect(css).toMatch(new RegExp(`\\.cm-md-image-portrait img ?\\{[^}]*max-height: ?min\\(${PORTRAIT_CAP_SVH}svh, ${PORTRAIT_CAP_PX}px\\)`));
		expect(css).not.toMatch(/\.cm-md-image img ?\{[^}]*max-height/);
	});

	it('turns transitions off inside the editor under reduced motion', () => {
		// CodeMirror measures in the task it writes in; a transition on every
		// property, however short, leaves the old geometry in the layout.
		const view = new EditorView({ state: EditorState.create({ doc: 'body', extensions: [docTheme] }), parent: document.body });
		const css = mountedCss().replace(/\s+/g, ' ');
		view.destroy();
		expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\) ?\{[^@]*\.cm-scroller, [^{]*\.cm-scroller \* ?\{ ?transition-property: none !important;? ?\}/);
	});

	it('sets the text you edit at phone width to the input size, on selectors a browser applies', () => {
		// A phone's browser zooms the page when an editable under the input size takes focus. A bare '&' inside @media renders
		// as a rule with no selector, which no browser applies, so the size has to name elements inside the editor.
		const view = new EditorView({ state: EditorState.create({ doc: 'body', extensions: [docTheme] }), parent: document.body });
		const css = mountedCss().replace(/\s+/g, ' ');
		view.destroy();
		expect(css).toMatch(/@media \(max-width: 768px\) ?\{[^@]*\.cm-scroller, [^{]*\.cm-md-table table ?\{ ?font-size: var\(--text-input\)/);
		expect(css).not.toMatch(/@media[^{]*\{ ?font-size/);
	});

	it('keeps room for the narrow floating offer but uses ordinary table padding on touch', () => {
		const view = new EditorView({ state: EditorState.create({ doc: 'body', extensions: [docTheme] }), parent: document.body });
		const css = mountedCss().replace(/\s+/g, ' ');
		view.destroy();
		const narrow = css.match(/@media \(max-width: 768px\) ?\{[^@]*?\.cm-md-table ?\{ ?padding-bottom: ([^;}]+)/);
		const coarse = css.match(/@media \(pointer: coarse\) ?\{[^@]*?\.cm-md-table ?\{ ?padding-bottom: ([^;}]+)/);
		expect(narrow?.[1]).toBe('calc(var(--touch-target) + 0.6em)');
		expect(coarse?.[1]).toBe('0.6em');
		expect(coarse!.index).toBeGreaterThan(narrow!.index!);
	});

	it('paints the selection from the accent selection role, never a share chosen here, and keeps the matched-text wash faint', () => {
		const view = new EditorView({ state: EditorState.create({ doc: 'body', extensions: [docTheme] }), parent: document.body });
		const css = mountedCss().replace(/\s+/g, ' ');
		view.destroy();
		expect(css).toMatch(/::selection ?\{ ?background-color: var\(--accent-selection\)/);
		expect(css).not.toMatch(/::selection ?\{[^}]*color-mix/);
		expect(css).toMatch(/\.cm-selectionMatch ?\{ ?background-color: var\(--accent-wash\)/);
	});

	it("stops a scrolled-to line where the canvas's own scroll padding does, and nowhere else", () => {
		// DocViewer.css sets the scroller's scroll-padding-bottom from the notice flags; the editor reads that one value back.
		const rules = document.createElement('style');
		rules.textContent = '.doc[data-notice-jump] .cm-scroller { scroll-padding-bottom: 60px; } .doc[data-notice-jump][data-notice-below] .cm-scroller { scroll-padding-bottom: 108px; }';
		document.head.appendChild(rules);
		const canvas = document.createElement('div');
		canvas.className = 'doc';
		document.body.appendChild(canvas);
		const view = new EditorView({ state: EditorState.create({ doc: 'body', extensions: [docScrollMargin] }), parent: canvas });
		const bottom = () => view.state.facet(EditorView.scrollMargins).map((margin) => margin(view)?.bottom ?? 0).reduce((a, b) => a + b, 0);
		expect(bottom()).toBe(0);
		canvas.setAttribute('data-notice-jump', '');
		expect(bottom()).toBe(60);
		canvas.setAttribute('data-notice-below', '');
		expect(bottom()).toBe(108);
		canvas.removeAttribute('data-notice-jump');
		canvas.removeAttribute('data-notice-below');
		expect(bottom()).toBe(0);
		view.destroy();
		canvas.remove();
		rules.remove();
		const bare = new EditorView({ state: EditorState.create({ doc: 'body', extensions: [docScrollMargin] }), parent: document.body });
		expect(bare.state.facet(EditorView.scrollMargins).map((margin) => margin(bare))).toEqual([null]);
		bare.destroy();
	});
});
