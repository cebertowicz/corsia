import { Decoration, EditorView, keymap } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';
import { Facet, Prec, type EditorState, type Text } from '@codemirror/state';
import type { SyntaxNode } from '@lezer/common';
import { resolveImagePath } from './markdown-image';

/** Supplied by the editor's host; with none, a link is only styled and never opens. */
export const linkOpener = Facet.define<(href: string) => void, ((href: string) => void) | null>({
	combine: (openers) => openers[0] ?? null,
});

/** The target of a lezer `Link` node as written, with a `<...>` wrapper removed; null for a link that has none (`[a][ref]`). */
function linkHref(node: SyntaxNode, doc: Text): string | null {
	const url = node.getChild('URL');
	const href = url ? doc.sliceString(url.from, url.to).trim().replace(/^<(.*)>$/, '$1') : '';
	return href || null;
}

/** What the person sees on hover: a web link as host and path, any other target as it was written. */
export function linkTitle(href: string): string {
	try {
		const url = new URL(href);
		if (url.protocol === 'http:' || url.protocol === 'https:') return `${url.host}${url.pathname === '/' ? '' : url.pathname}`;
	} catch {
		// Not an absolute URL: it is a path or another scheme, shown as written.
	}
	return href;
}

/** An in-document anchor (`#heading`) is not followed; a click on it places the caret as before. */
function followable(href: string | null): href is string {
	return href !== null && !href.startsWith('#');
}

/** The decoration of a `Link` node: styled always, and a pointer with its destination where an opener exists. */
export function linkDecoration(state: EditorState, node: SyntaxNode): Decoration {
	const href = linkHref(node, state.doc);
	if (!followable(href) || !state.facet(linkOpener)) return Decoration.mark({ class: 'cm-md-link' });
	return Decoration.mark({ class: 'cm-md-link cm-md-link-open', attributes: { title: linkTitle(href) } });
}

/** The `Link` node holding `pos`, its ends included, read from the syntax tree at the moment of use. */
function linkNodeAt(state: EditorState, pos: number): SyntaxNode | null {
	for (const side of [1, -1] as const) {
		for (let node: SyntaxNode | null = syntaxTree(state).resolveInner(pos, side); node; node = node.parent) {
			if (node.name === 'Link') return node;
		}
	}
	return null;
}

/** The followable target of the link a pointer event landed on, or null. */
function hrefUnder(view: EditorView, target: EventTarget | null): string | null {
	const mark = (target as Element | null)?.closest?.('.cm-md-link-open');
	if (!mark || !view.contentDOM.contains(mark)) return null;
	const node = linkNodeAt(view.state, view.posAtDOM(mark));
	const href = node ? linkHref(node, view.state.doc) : null;
	return followable(href) ? href : null;
}

/**
 * A relative target resolves from the document's folder, which is how an agent writes links between documents.
 * Anything else (a web or other scheme target, an absolute path, `/thread/...`, `/settings/...`) goes as written.
 */
export function documentLinkTarget(docPath: string | null, href: string): string {
	if (href.startsWith('/') || href.startsWith('#')) return href;
	const resolved = resolveImagePath(docPath, href.split(/[?#]/, 1)[0]);
	return resolved ? resolved.split('/').map(encodeURIComponent).join('/') : href;
}

/**
 * Pressing link text opens the link and leaves the caret, selection and document as they were; a Shift-press still
 * extends the selection. A drag that starts outside the link never fires the click, so it selects across the link.
 */
export const openLinkOnClick = EditorView.domEventHandlers({
	mousedown(event, view) {
		if (event.button !== 0 || event.shiftKey || !hrefUnder(view, event.target)) return false;
		event.preventDefault();
		return true;
	},
	click(event, view) {
		const open = view.state.facet(linkOpener);
		const href = event.button === 0 && !event.shiftKey ? hrefUnder(view, event.target) : null;
		if (!open || href === null) return false;
		event.preventDefault();
		open(href);
		return true;
	},
});

/** Alt-Enter with the caret in a link opens it, so the keyboard reaches what a click does; Mod-Enter stays the task toggle. */
export const openLinkOnAltEnter = Prec.high(keymap.of([{
	key: 'Alt-Enter',
	run(view) {
		const open = view.state.facet(linkOpener);
		const node = open ? linkNodeAt(view.state, view.state.selection.main.head) : null;
		const href = node ? linkHref(node, view.state.doc) : null;
		if (!open || !followable(href)) return false;
		open(href);
		return true;
	},
}]));
