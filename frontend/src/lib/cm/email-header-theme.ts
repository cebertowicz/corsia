import { EditorView } from '@codemirror/view';

// The look of the compact email header: the CodeMirror theme for the lines and widgets `email-header.ts` draws.

export const emailHeaderTheme = EditorView.theme({
	// A wrapped value hangs under the value column, not under the label.
	'.cm-line.cm-email-head': { fontSize: 'var(--text-footnote)', lineHeight: 'var(--lh-normal)', color: 'var(--text-secondary)', paddingLeft: 'calc(5.2em + 0.27em + 2px)', textIndent: 'calc(-5.2em - 0.27em)' },
	// The Cc button sits at the right end of the To line; the line keeps room for it, so a long address clips before it.
	'.cm-line.cm-email-head-to-cc': { position: 'relative', paddingRight: '2.6em' },
	'.cm-email-cc-add': {
		all: 'unset', boxSizing: 'border-box', cursor: 'pointer', position: 'absolute', right: '2px', top: '0', zIndex: '1',
		padding: '0 var(--space-1)', textIndent: '0', borderRadius: 'var(--radius-sm)', color: 'var(--text-tertiary)',
		fontSize: 'var(--text-footnote)', lineHeight: 'var(--lh-normal)',
		'&:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '1px' },
	},
	'.cm-line.cm-email-head-empty': { borderRadius: 'var(--radius-sm)', boxShadow: 'inset 0 0 0 1px var(--border-subtle)' },
	// An address that was not on the email being answered: amber, with a "!" that says so.
	'.cm-email-added, .cm-email-added-tail': { color: 'var(--amber-text)' },
	'.cm-email-added-tail': { whiteSpace: 'nowrap' },
	'.cm-email-added-mark': {
		all: 'unset', boxSizing: 'border-box', cursor: 'help', position: 'relative', display: 'inline-block', textIndent: '0', margin: '0 0 0 var(--space-1)',
		minWidth: '1.15em', textAlign: 'center', borderRadius: '999px', boxShadow: 'inset 0 0 0 1px var(--amber-text)', color: 'var(--amber-text)',
		fontSize: 'var(--text-footnote)', lineHeight: '1.15', fontWeight: 'var(--fw-semibold)',
		'&:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '1px' },
	},
	'.cm-email-added-tip': {
		// On the mark's own row, centered on it, one line unless the room is narrower than the text (see AddedMarkWidget). The ring is a shadow so it adds no height.
		display: 'none', position: 'absolute', top: '50%', transform: 'translateY(-50%)', zIndex: '5', width: 'max-content',
		padding: 'calc(var(--space-1) / 4) var(--space-2)', borderRadius: 'var(--radius-sm)', background: 'var(--bg-elevated)', color: 'var(--text-primary)', lineHeight: '1.15',
		boxShadow: 'var(--shadow-chip), 0 0 0 1px var(--border-subtle)', fontWeight: 'var(--fw-regular)', textAlign: 'left', whiteSpace: 'nowrap', cursor: 'default',
	},
	'.cm-email-added-tip[data-side="right"]': { left: '100%', marginLeft: 'var(--space-1)' },
	'.cm-email-added-tip[data-side="left"]': { right: '100%' },
	'.cm-email-added-mark[data-open] .cm-email-added-tip, .cm-email-added-mark:focus-visible .cm-email-added-tip': { display: 'block' },
	'.cm-email-address-hint': { textIndent: '0', color: 'var(--text-tertiary)', pointerEvents: 'none', userSelect: 'none' },
	'.cm-email-head-label': { display: 'inline-block', minWidth: '5.2em', textIndent: '0', color: 'var(--text-tertiary)' },
	'.cm-email-from': {
		padding: '0 2px', fontSize: 'var(--text-footnote)', lineHeight: 'var(--lh-normal)', color: 'var(--text-tertiary)',
		cursor: 'default', whiteSpace: 'pre', overflow: 'hidden', textOverflow: 'ellipsis',
	},
	'.cm-line.cm-email-head-last': { borderBottom: '1px solid var(--border-subtle)', paddingBottom: 'var(--space-2)' },
	'.cm-line.cm-email-head-before-notes': { paddingBottom: 'var(--space-1)' },
	'.cm-line.cm-email-head-gap': { height: 'var(--space-3)', lineHeight: '0', overflow: 'hidden' },
	'.cm-email-attachments': { padding: '0 2px' },
	'.cm-email-notes': { borderBottom: '1px solid var(--border-subtle)', padding: '0 2px var(--space-2)', fontSize: 'var(--text-footnote)', lineHeight: 'var(--lh-normal)', color: 'var(--text-tertiary)' },
	'.cm-email-notes-toggle': {
		all: 'unset', boxSizing: 'border-box', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 'var(--space-1)',
		borderRadius: 'var(--radius-sm)', color: 'var(--text-tertiary)',
		'&:focus-visible': { outline: '2px solid var(--accent)', outlineOffset: '2px' },
		'& svg': { transition: 'transform var(--dur-tight)' },
		'&[aria-expanded="true"] svg': { transform: 'rotate(90deg)' },
	},
	'@media (hover: hover)': { '.cm-email-added-mark:hover .cm-email-added-tip': { display: 'block' }, '.cm-email-notes-toggle:hover': { color: 'var(--text-primary)' }, '.cm-email-cc-add:hover': { color: 'var(--text-primary)' } },
	'.cm-email-notes-list': { margin: 'var(--space-1) 0 0', padding: '0', listStyle: 'none', overflowWrap: 'anywhere', '& li': { margin: '0 0 var(--space-1)' } },
	// On a phone the line keeps wrapping (the default is already wrapping on the desktop): a long address breaks anywhere, never cut.
	'@media (max-width: 48rem)': { '.cm-line.cm-email-head-address': { whiteSpace: 'pre-wrap', overflow: 'visible', overflowWrap: 'anywhere' } },
	// A finger needs more than a footnote-sized word: the hit area grows past the line without moving the text.
	'@media (pointer: coarse)': { '.cm-email-notes-toggle': { minHeight: 'var(--touch-target)' }, '.cm-email-cc-add::after': { content: '""', position: 'absolute', inset: '-10px -6px' }, '.cm-email-added-mark::after': { content: '""', position: 'absolute', inset: '-12px -8px' } },
	'@media (prefers-reduced-motion: reduce)': { '.cm-email-notes-toggle svg': { transition: 'none' } },
});
