import { markdown } from '@codemirror/lang-markdown';
import { Autolink, Strikethrough } from '@lezer/markdown';

/**
 * The markdown a table cell's own editor parses. A cell holds inline content only, as GFM parses a cell: no block construct
 * is parsed, so `-`, `# x`, `---`, `> q` or `1. x` are the literal text the table draws, and no markdown key (Enter
 * continuing a list) outranks the cell's own keys.
 */
export const INLINE_MARKDOWN = markdown({
	addKeymap: false,
	extensions: [Strikethrough, Autolink, {
		remove: ['Blockquote', 'BulletList', 'OrderedList', 'ATXHeading', 'SetextHeading', 'HorizontalRule', 'FencedCode', 'IndentedCode', 'HTMLBlock', 'LinkReference']
	}]
});
