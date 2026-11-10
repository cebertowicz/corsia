import '../components/commentAnchors.css';
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
		padding: '4px var(--doc-phone-inset, 0px) 40vh var(--doc-inset, 24px)',
		caretColor: 'var(--accent)',
		maxWidth: '100%'
	},
	// The text's left inset is --doc-inset (24px; the image viewer sets 0), the document header takes the same. --doc-phone-inset
	// is set once on the canvas (DocViewer.css) for a touch pointer at phone width, so the text keeps that inset on the right too.
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
	// A cell's own editor is the one thing inside a table that is written to, so it takes back what the rule above takes
	// away; WebKit honours `contenteditable` only where user-modify allows it.
	'.cm-md-cell-editor, .cm-md-cell-editor *': { userSelect: 'text', WebkitUserModify: 'read-write' },
	// While a cell is open its editor holds the reader's place. The document's own caret, mirrored to the same source range
	// for the comment flows, would otherwise be a second caret on the same text.
	'&.cm-md-cell-open .cm-cursorLayer': { display: 'none' },
	'.cm-md-table .table-wrap': { margin: '0' },
	// An image sits at its own size, scaled down to the column and never up; padding, not margin, as above.
	'.cm-md-image': { padding: '0.4em 0' },
	'.cm-md-image img': { display: 'block', maxWidth: '100%', height: 'auto' },
	'.cm-md-image-selected img, .cm-md-image-selected .cm-md-image-note': { outline: '2px solid var(--accent)', outlineOffset: '-2px' },
	'.cm-md-image-note': { color: 'var(--text-tertiary)' },
	// A cell image or diagram is a tab stop that Enter or Space opens (`widget-open.ts`), ringed as the selected image is.
	'.cm-md-cell-image:focus-visible, .cm-md-mermaid:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '2px' },

	// Text anchor rules live in commentAnchors.css for documents and chat.
	// A whole table or diagram: the same amber as an outline on the block, since a block has no text to underline.
	'.cm-comment-block': { outline: 'medium solid color-mix(in srgb, var(--amber) 42%, transparent)', outlineOffset: 'calc(var(--space-1) * -1)', transition: 'outline-color var(--dur-tight)' },
	'.cm-comment-block-pending': { outline: 'medium dashed var(--amber)', outlineOffset: 'calc(var(--space-1) * -1)' },
	'.cm-comment-block-focused': { outlineColor: 'var(--amber)' },
	// A mark on a whole table cell: the text highlight's amber underline, drawn inside the cell so its own border stays as it is.
	'.cm-md-table td.cm-comment-anchor, .cm-md-table th.cm-comment-anchor': { boxShadow: 'inset 0 -2px 0 color-mix(in srgb, var(--amber) 42%, transparent)' },
	'.cm-md-table td.cm-comment-focused, .cm-md-table th.cm-comment-focused, .cm-md-table td.cm-comment-pending, .cm-md-table th.cm-comment-pending': { boxShadow: 'inset 0 -2px 0 var(--amber)' },
	// The rail is a positioned layer holding one empty element per commented line, which the margin cards read their line from;
	// it draws nothing and takes no pointer or focus.
	'.cm-comment-rail': { position: 'absolute', left: '0', top: '0', width: '0', height: '0', zIndex: '1', pointerEvents: 'none', visibility: 'hidden' },
	'.cm-comment-line': { position: 'absolute', left: '0', width: '0' },
	// An image or diagram comment's pin on the image in the text (`image-pins.ts`): the image viewer's 22 px disc in its author's
	// colour (ImageRegionLayer), centred where the viewer places it, with a 44 px target and the viewer's selected ring.
	'.cm-image-pins': { position: 'absolute', left: '0', top: '0', width: '0', height: '0', zIndex: '2' },
	'.cm-image-pin': { position: 'absolute', padding: '0', border: '0', background: 'none', font: 'inherit', cursor: 'pointer', transform: 'translate(-11px, -11px)', WebkitTapHighlightColor: 'transparent' },
	'.cm-image-pin::before': { content: '""', position: 'absolute', left: '-11px', top: '-11px', width: '44px', height: '44px' },
	'.cm-image-pin .disc': {
		position: 'relative', boxSizing: 'border-box', display: 'grid', placeItems: 'center', width: '22px', height: '22px', borderRadius: 'var(--radius-circle)',
		background: 'color-mix(in srgb, var(--disc-color) 80%, transparent)', color: 'var(--image-region-ink)',
		border: '1.5px solid color-mix(in srgb, var(--region-color) 70%, var(--image-region-edge))', boxShadow: '0 0 0 1.5px var(--image-region-halo)',
		fontSize: 'var(--text-caption)', fontWeight: 'var(--fw-bold)', fontVariantNumeric: 'tabular-nums', lineHeight: '1',
	},
	'.cm-image-pin .disc::after': {
		content: '""', position: 'absolute', inset: '-5.5px', borderRadius: 'var(--radius-circle)', pointerEvents: 'none',
		boxShadow: 'inset 0 0 0 2.5px var(--region-color)', opacity: '0', transition: 'opacity var(--dur-tight) var(--ease-snap)',
	},
	'.cm-image-pin.selected .disc::after': { opacity: '1' },
	// A deleted passage's sign (900 and 393, where no card shows): the dashed amber edge its margin card has, on the page colour.
	'.cm-image-pin.removed .disc': { background: 'var(--bg-primary)', border: '1.5px dashed var(--amber)', boxShadow: 'none' },
	'.cm-image-pin:focus-visible': { outline: 'none' },
	'.cm-image-pin:focus-visible .disc': { outline: '2px solid var(--accent)', outlineOffset: '3px' },
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
