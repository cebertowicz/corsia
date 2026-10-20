import { EditorView } from '@codemirror/view';
import { HighlightStyle, syntaxHighlighting } from '@codemirror/language';
import { tags as t } from '@lezer/highlight';

export const docTheme = EditorView.theme({
	'&': {
		color: 'var(--text-primary)',
		backgroundColor: 'transparent',
		fontSize: '15px',
		height: '100%'
	},
	'.cm-scroller': {
		fontFamily: 'var(--font-sans)',
		lineHeight: 'var(--lh-relaxed)',
		overflow: 'auto'
	},
	'.cm-content': {
		padding: '4px var(--doc-phone-inset, 0px) 40vh var(--doc-phone-inset, var(--doc-rail, 44px))',
		caretColor: 'var(--accent)',
		maxWidth: '100%'
	},
	// --doc-phone-inset is set once on the canvas (DocViewer.css) for a touch pointer at phone width: the text keeps that
	// inset on both sides, and the 44px marker hit box reaches into its edge. Unset, the one-sided rail applies.
	'.cm-line': { padding: '0 2px' },
	'&.cm-focused': { outline: 'none' },
	'.cm-cursor, .cm-dropCursor': { borderLeftColor: 'var(--accent)', borderLeftWidth: '2px' },
	// The selection is the strongest wash in the editor: --accent-subtle is a 12% wash that sits at about 1.1:1 on the page,
	// too faint to see a drag, and it is also the matched-text wash below, so it is its own token rather than that one.
	// --accent-selection follows the accent (a project's, or the default) and comes from accentRoles() with every other
	// accent role: the share that reads against the page is per accent and theme, since the same percentage of a pale
	// colour reads far fainter than of a deep one. .cm-selectionMatch keeps the faint wash so a match never reads as
	// the selection. The editor draws no selection layer of its own (no drawSelection), so the native ::selection is
	// the only selection there is.
	'::selection': { backgroundColor: 'var(--accent-selection)' },
	'.cm-selectionMatch': { backgroundColor: 'var(--accent-wash)' },
	'.cm-gutters': { display: 'none' },

	'.cm-md-h1': { fontSize: '1.7em', fontWeight: '600', lineHeight: '1.25', letterSpacing: '-0.01em' },
	'.cm-md-h2': { fontSize: '1.38em', fontWeight: '600', lineHeight: '1.3', letterSpacing: '-0.01em' },
	'.cm-md-h3': { fontSize: '1.18em', fontWeight: '600', lineHeight: '1.35' },
	'.cm-md-h4': { fontSize: '1.04em', fontWeight: '600' },
	'.cm-md-h5': { fontSize: '0.95em', fontWeight: '700', textTransform: 'uppercase', letterSpacing: 'var(--ls-cap)', color: 'var(--text-secondary)' },
	'.cm-md-h6': { fontSize: '0.9em', fontWeight: '700', color: 'var(--text-tertiary)' },

	'.cm-md-quote': {
		color: 'var(--text-secondary)',
		fontStyle: 'italic',
		borderLeft: '3px solid var(--accent-line)',
		paddingLeft: '14px'
	},
	'.cm-md-code': {
		fontFamily: 'var(--font-mono)',
		fontSize: '0.88em',
		backgroundColor: 'var(--code-bg)'
	},
	'.cm-md-hr': { display: 'inline-block', width: '100%', borderBottom: '1px solid var(--border)', verticalAlign: 'middle' },

	'.cm-md-inline-code': {
		fontFamily: 'var(--font-mono)',
		fontSize: '0.86em',
		background: 'var(--code-bg)',
		border: '1px solid var(--border-subtle)',
		borderRadius: 'var(--radius-xs)',
		padding: '0.05em 0.35em'
	},
	'.cm-md-link': { color: 'var(--accent)', textDecoration: 'underline', textDecorationColor: 'var(--accent-line)' },
	'.cm-md-link-open': { transition: 'text-decoration-color 150ms' },
	'@media (hover: hover) and (pointer: fine)': { '.cm-md-link-open': { cursor: 'pointer' }, '.cm-md-link-open:hover': { textDecorationColor: 'var(--accent)' } },
	'@media (pointer: coarse)': { '.cm-md-link-open:active': { backgroundColor: 'var(--accent-wash)' } },
	'.cm-md-bullet': { color: 'var(--text-tertiary)' },
	'.cm-md-task-control': { display: 'inline-flex', width: '28px', height: '28px', margin: '-3px 4px -3px 0', verticalAlign: 'middle', alignItems: 'center', justifyContent: 'center' },
	'.cm-md-task-checkbox': { width: '18px', height: '18px', margin: '0', accentColor: 'var(--accent)', cursor: 'pointer' },
	'.cm-md-task-control:hover .cm-md-task-checkbox': { outline: '2px solid var(--accent-wash)', outlineOffset: '2px' },
	'.cm-md-task-checkbox:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '2px' },
	'.cm-md-task-checkbox:disabled': { cursor: 'default', opacity: '0.65' },
	'.cm-md-task-done': { textDecoration: 'line-through', color: 'var(--text-tertiary)' },

	// Space around a block widget is padding, never margin: CodeMirror measures a
	// widget's own box, so a margin shifts every line below it by that much
	// against the editor's height map and a click lands on the line underneath.
	// The diagram widget's spacing is beside the shared .mermaid-diagram rule in app.css.
	'.cm-md-table': { margin: '0', padding: '0.6em 0' },
	// Without this, a drag that starts inside a widget is turned into a caret on the editor, so no text in a table or diagram can be selected.
	'.cm-md-table, .cm-md-table *, .cm-md-mermaid, .cm-md-mermaid *': { userSelect: 'text', WebkitUserModify: 'read-only' },
	'.cm-md-table .table-wrap': { margin: '0' },
	// An image sits at its own size, scaled down to the column and never up; padding, not margin, as above.
	'.cm-md-image': { padding: '0.4em 0' },
	'.cm-md-image img': { display: 'block', maxWidth: '100%', height: 'auto' },
	'.cm-md-image-selected img, .cm-md-image-selected .cm-md-image-note': { outline: '2px solid var(--accent)', outlineOffset: '-2px' },
	'.cm-md-image-note': { color: 'var(--text-tertiary)' },

	'.cm-comment-anchor': {
		backgroundColor: 'color-mix(in srgb, var(--amber) 13%, transparent)',
		borderBottom: '2px solid color-mix(in srgb, var(--amber) 42%, transparent)',
		transition: 'background-color 0.15s'
	},
	// The captured range while its comment is being written: the anchor's amber with a dashed
	// underline, so an unposted draft never reads as a placed or focused comment.
	'.cm-comment-pending': {
		backgroundColor: 'color-mix(in srgb, var(--amber) 20%, transparent)',
		borderBottom: '2px dashed var(--amber)'
	},
	'.cm-comment-focused': {
		backgroundColor: 'color-mix(in srgb, var(--amber) 26%, transparent)',
		borderBottomColor: 'var(--amber)'
	},
	// A whole table or diagram: the same amber as an outline on the block, since a block has no text to underline.
	'.cm-comment-block': { outline: 'medium solid color-mix(in srgb, var(--amber) 42%, transparent)', outlineOffset: 'calc(var(--space-1) * -1)', transition: 'outline-color var(--dur-tight)' },
	'.cm-comment-block-pending': { outline: 'medium dashed var(--amber)', outlineOffset: 'calc(var(--space-1) * -1)' },
	'.cm-comment-block-focused': { outlineColor: 'var(--amber)' },
	// A mark on a whole table cell: the same underline, as a cell's own bottom edge.
	'.cm-md-table td.cm-comment-anchor, .cm-md-table th.cm-comment-anchor': { borderBottomWidth: 'medium' },
	// Markers live in a rail the content padding reserves (--doc-rail: 24px for a fine pointer,
	// 44px for a coarse or unknown one, decided by railWidth() and set by the rail and the viewer;
	// the document header takes the same inset, and every CSS fallback is the unknown-pointer 44px). The rail is a
	// positioned layer, not a gutter: it adds no inline advance to any line, so text starts at the
	// rail's inner edge whether or not a line is commented. Each button is the whole hit box; the
	// glyph is 18px inside it.
	'.cm-comment-rail': { position: 'absolute', left: '0', top: '0', width: '0', height: '0', zIndex: '1', pointerEvents: 'none' },
	'.cm-comment-marker': {
		position: 'absolute', boxSizing: 'border-box', display: 'flex', alignItems: 'flex-start', justifyContent: 'center', margin: '0', padding: '0', border: '0', background: 'none',
		color: 'var(--amber-text)', fontFamily: 'inherit', pointerEvents: 'auto', cursor: 'pointer'
	},
	'.cm-comment-marker svg': { borderRadius: 'var(--radius-xs)', transition: 'background-color 0.12s' },
	'.cm-comment-count': {
		boxSizing: 'border-box', display: 'flex', alignItems: 'center', justifyContent: 'center', width: '18px', height: '18px', overflow: 'hidden',
		border: '1.5px solid currentColor', borderRadius: 'var(--radius-xs)', fontSize: '11px', fontWeight: 'var(--fw-semibold)', lineHeight: '1', fontVariantNumeric: 'tabular-nums',
		transition: 'background-color 0.12s'
	},
	// The count box is 18px, centred, at the button's top: centre the 6px dot on its top-right corner.
	'.cm-comment-dot': { position: 'absolute', top: '-3px', left: 'calc(50% + 6px)', width: '6px', height: '6px', borderRadius: '50%', backgroundColor: 'currentColor' },
	'.cm-comment-marker:hover svg, .cm-comment-marker:hover .cm-comment-count': { backgroundColor: 'color-mix(in srgb, var(--amber) 16%, transparent)' },
	'.cm-comment-marker:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '-2px' },
	// The app's reduced-motion reset gives every element a 0.01ms transition on
	// every property, so a height or class the editor has just written is not in
	// the layout until the next frame. CodeMirror measures in the same task it
	// writes in; it would record the old geometry and place clicks by it.
	'@media (prefers-reduced-motion: reduce)': { '.cm-scroller, .cm-scroller *': { transitionProperty: 'none !important' } },
	'@media (max-width: 768px)': { '&': { fontSize: '16px' }, '.cm-md-task-control': { width: '44px', height: '44px', margin: '-9px 2px -9px -8px' } }
});

export const docHighlight = syntaxHighlighting(
	HighlightStyle.define([
		{ tag: t.heading, color: 'var(--text-primary)' },
		{ tag: t.strong, fontWeight: '600', color: 'var(--text-primary)' },
		{ tag: t.emphasis, fontStyle: 'italic' },
		{ tag: t.strikethrough, textDecoration: 'line-through', color: 'var(--text-tertiary)' },
		{ tag: t.link, color: 'var(--accent)' },
		{ tag: t.url, color: 'var(--text-tertiary)' },
		{ tag: t.monospace, fontFamily: 'var(--font-mono)' },
		{ tag: t.quote, color: 'var(--text-secondary)' },
		{ tag: [t.keyword, t.tagName], color: 'var(--hl-keyword)' },
		{ tag: [t.string, t.special(t.string)], color: 'var(--hl-string)' },
		{ tag: t.comment, color: 'var(--hl-comment)' },
		{ tag: [t.number, t.bool, t.atom], color: 'var(--hl-number)' },
		{ tag: t.list, color: 'var(--text-primary)' }
	])
);
