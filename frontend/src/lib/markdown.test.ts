import { describe, it, expect } from 'vitest';
import { renderMarkdown } from './markdown';

// renderMarkdown is the single trust boundary for every `{@html renderMarkdown(...)}`
// consumer (chat, tool-activity, conversation viewer, the documents table widget).
describe('renderMarkdown — sanitization (the choke point)', () => {
	it('strips <script> tags from the output', () => {
		const html = renderMarkdown('hello\n\n<script>window.__x = 1</script>\n\nworld').toLowerCase();
		expect(html).not.toContain('<script');
		expect(html).toContain('hello');
	});

	it('strips inline event handlers', () => {
		const html = renderMarkdown('text <img src=x onerror="window.__x = 1"> more').toLowerCase();
		expect(html).not.toContain('onerror');
	});

	it('strips javascript: URLs from links', () => {
		const html = renderMarkdown('[click me](javascript:window.__x=1)').toLowerCase();
		expect(html).not.toContain('javascript:');
	});

	it('neutralizes script/handler payloads inside a GFM table cell', () => {
		const table = ['| Item | Payload |', '|------|---------|', '| ok | <img src=x onerror="y()"> |', '| <script>z()</script> | done |'].join('\n');
		const html = renderMarkdown(table).toLowerCase();
		expect(html).toContain('<table');
		expect(html).not.toContain('onerror');
		expect(html).not.toContain('<script');
		expect(html).toContain('done');
	});
});

describe('renderMarkdown — legitimate markup survives sanitization', () => {
	it('keeps bold and table structure incl. alignment', () => {
		expect(renderMarkdown('**bold**')).toContain('<strong>bold</strong>');
		const tbl = renderMarkdown('| A | B |\n|:-:|--:|\n| x | y |');
		expect(tbl).toContain('<table');
		expect(tbl).toContain('text-align:center');
		expect(tbl).toContain('text-align:right');
	});

	it('renders a code block with a copy button and NO inline onclick', () => {
		const html = renderMarkdown('```js\nconst a = 1;\n```');
		expect(html).toContain('class="code-block"');
		expect(html).toContain('copy-btn');
		expect(html.toLowerCase()).not.toContain('onclick');
	});

	it('keeps target=_blank and forces rel=noopener on rendered links', () => {
		const html = renderMarkdown('[site](https://example.com)');
		expect(html).toContain('href="https://example.com"');
		expect(html).toContain('target="_blank"');
		expect(html).toContain('rel="noopener');
	});

	it('forces rel=noopener even on a raw-HTML target=_blank link', () => {
		const html = renderMarkdown('a <a href="https://e.com" target="_blank">raw</a> link');
		expect(html).toContain('target="_blank"');
		expect(html).toContain('rel="noopener');
	});
});

describe('renderMarkdown — table scroll contract (#483)', () => {
	it('wraps table in .table-wrap for horizontal scrolling', () => {
		const src = '| Col1 | Col2 | Col3 |\n|------|------|------|\n| a | b | c |';
		const html = renderMarkdown(src);
		// .table-wrap must be the direct parent so overflow-x:auto on the wrapper scrolls the table
		expect(html).toContain('<div class="table-wrap"><table>');
	});

	it('does NOT put overflow-x or display:block on the table element itself', () => {
		const src = '| A | B |\n|---|---|\n| x | y |';
		const html = renderMarkdown(src);
		// Table element must not carry display:block or overflow-x — those belong on .table-wrap
		expect(html).not.toMatch(/<table[^>]+style="[^"]*display\s*:\s*block/i);
		expect(html).not.toMatch(/<table[^>]+style="[^"]*overflow/i);
	});
});
