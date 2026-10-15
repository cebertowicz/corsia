import { Decoration, type DecorationSet, EditorView, ViewPlugin, type ViewUpdate, WidgetType } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';
import { StateField, type EditorState, type Extension, type Range } from '@codemirror/state';
import type { SyntaxNodeRef } from '@lezer/common';
import { renderMarkdown } from '$lib/markdown';
import { mountMermaid, type MermaidMount } from '$lib/mermaid';
import { markdownImages, openSelectedImageOnEnter, renderableImage, selectImageOnClick } from './markdown-image';
import { linkDecoration, openLinkOnAltEnter, openLinkOnClick } from './markdown-link';

class BulletWidget extends WidgetType {
	toDOM() {
		const s = document.createElement('span');
		s.className = 'cm-md-bullet';
		s.textContent = '• ';
		return s;
	}
	eq() { return true; }
}

class HrWidget extends WidgetType {
	toDOM() {
		const s = document.createElement('span');
		s.className = 'cm-md-hr';
		return s;
	}
	eq() { return true; }
}

/**
 * Renders a GFM table block as a real HTML table, reusing the app's canonical
 * markdown renderer so inline formatting and theming match everywhere else.
 * The table always stays rendered — it never drops to raw pipe/dash source.
 */
class TableWidget extends WidgetType {
	constructor(readonly src: string) { super(); }
	eq(o: TableWidget) { return o.src === this.src; }
	toDOM() {
		const el = document.createElement('div');
		el.className = 'cm-md-table md-content';
		// Same trust boundary as every `{@html} renderMarkdown(...)` surface:
		// renderMarkdown applies the canonical DOMPurify policy before HTML reaches
		// the document. Keep this widget on that single sanitized renderer.
		el.innerHTML = renderMarkdown(this.src);
		return el;
	}
	ignoreEvent() { return true; }
}

// Per-DOM mount controllers so each widget can re-render on theme change and
// clean up its theme subscription when CodeMirror destroys the node.
const mermaidMounts = new WeakMap<HTMLElement, MermaidMount>();

/**
 * Replaces a ```mermaid fenced block with a rendered SVG diagram. Like
 * TableWidget, the block stays rendered regardless of cursor position. Mermaid
 * renders asynchronously and owns its own output safety (`securityLevel:
 * 'strict'`); a malformed diagram shows an inline error rather than blanking the
 * doc. See `$lib/mermaid`.
 */
class MermaidWidget extends WidgetType {
	constructor(readonly code: string) { super(); }
	eq(o: MermaidWidget) { return o.code === this.code; }
	toDOM() {
		const el = document.createElement('div');
		el.className = 'cm-md-mermaid';
		mermaidMounts.set(el, mountMermaid(el, this.code));
		return el;
	}
	destroy(dom: HTMLElement) {
		mermaidMounts.get(dom)?.destroy();
		mermaidMounts.delete(dom);
	}
	ignoreEvent() { return true; }
}

/**
 * If `node` is a ```mermaid fenced code block, returns its body source; else
 * null. Reads the FencedCode node's CodeInfo (language) and CodeText children.
 */
function mermaidFence(node: SyntaxNodeRef, doc: EditorState['doc']): string | null {
	const info = node.node.getChild('CodeInfo');
	if (!info) return null;
	if (doc.sliceString(info.from, info.to).trim().toLowerCase() !== 'mermaid') return null;
	const text = node.node.getChild('CodeText');
	return text ? doc.sliceString(text.from, text.to) : '';
}

const HEADING = /^ATXHeading(\d)$/;

/**
 * Hides markdown syntax marks so the doc reads as rendered prose. Marks stay
 * hidden regardless of cursor position — the rendered doc never reveals raw
 * markdown. Table interiors are skipped here; they are replaced wholesale by
 * the block-level table decorations below.
 */
function buildInlineDecorations(view: EditorView): DecorationSet {
	const decos: Range<Decoration>[] = [];
	const { doc } = view.state;
	const hide = (from: number, to: number, widget?: WidgetType) => {
		if (from >= to) return;
		decos.push((widget ? Decoration.replace({ widget }) : Decoration.replace({})).range(from, to));
	};

	for (const { from, to } of view.visibleRanges) {
		syntaxTree(view.state).iterate({
			from, to,
			enter: (node) => {
				const name = node.name;
				if (name === 'Table') return false; // rendered as a block widget instead
				const hm = HEADING.exec(name);
				if (hm) {
					const line = doc.lineAt(node.from);
					decos.push(Decoration.line({ class: `cm-md-h${hm[1]}` }).range(line.from));
					return;
				}
				switch (name) {
					case 'HeaderMark': {
						const next = doc.sliceString(node.to, node.to + 1) === ' ' ? node.to + 1 : node.to;
						hide(node.from, next);
						break;
					}
					case 'QuoteMark': {
						const line = doc.lineAt(node.from);
						decos.push(Decoration.line({ class: 'cm-md-quote' }).range(line.from));
						const next = doc.sliceString(node.to, node.to + 1) === ' ' ? node.to + 1 : node.to;
						hide(node.from, next);
						break;
					}
					case 'EmphasisMark':
					case 'StrikethroughMark':
					case 'CodeMark':
						hide(node.from, node.to);
						break;
					case 'InlineCode':
						decos.push(Decoration.mark({ class: 'cm-md-inline-code' }).range(node.from, node.to));
						break;
					case 'ListMark': {
						const txt = doc.sliceString(node.from, node.to);
						if (/^[-*+]$/.test(txt) && !node.node.parent?.getChild('Task')) {
							const next = doc.sliceString(node.to, node.to + 1) === ' ' ? node.to + 1 : node.to;
							hide(node.from, next, new BulletWidget());
						}
						break;
					}
					case 'Link':
						decos.push(linkDecoration(view.state, node.node).range(node.from, node.to));
						break;
					case 'Image':
						if (renderableImage(view.state, node.node)) return false; // hidden and drawn by `markdown-image.ts`
						decos.push(Decoration.mark({ class: 'cm-md-link' }).range(node.from, node.to)); // a web URL shows as a link, never fetched
						break;
					case 'LinkMark':
					case 'URL':
						hide(node.from, node.to);
						break;
					case 'FencedCode': {
						if (mermaidFence(node, doc) !== null) return false; // rendered as a block widget instead
						const a = doc.lineAt(node.from).number;
						const b = doc.lineAt(Math.min(node.to, doc.length)).number;
						for (let n = a; n <= b; n++) decos.push(Decoration.line({ class: 'cm-md-code' }).range(doc.line(n).from));
						break;
					}
					case 'HorizontalRule':
						hide(node.from, node.to, new HrWidget());
						break;
				}
			}
		});
	}
	return Decoration.set(decos, true);
}

const markdownInlinePreview = ViewPlugin.fromClass(
	class {
		decorations: DecorationSet;
		constructor(view: EditorView) {
			this.decorations = buildInlineDecorations(view);
		}
		update(u: ViewUpdate) {
			if (u.docChanged || u.viewportChanged || syntaxTree(u.state) !== syntaxTree(u.startState)) {
				this.decorations = buildInlineDecorations(u.view);
			}
		}
	},
	{ decorations: (v) => v.decorations }
);

/**
 * Replaces each GFM table with a rendered block widget. Block decorations must
 * come from a state field (CodeMirror forbids them from view plugins), and the
 * replaced range spans whole lines so the widget cleanly takes the table's place.
 */
function buildTableDecorations(state: EditorState): DecorationSet {
	const decos: Range<Decoration>[] = [];
	const { doc } = state;
	syntaxTree(state).iterate({
		enter: (node) => {
			if (node.name !== 'Table') return;
			const from = doc.lineAt(node.from).from;
			const to = doc.lineAt(Math.min(node.to, doc.length)).to;
			const src = doc.sliceString(from, to);
			decos.push(Decoration.replace({ widget: new TableWidget(src), block: true }).range(from, to));
			return false;
		}
	});
	return Decoration.set(decos, true);
}

/**
 * A block-decoration StateField whose set is derived from the syntax tree.
 * Rebuilds when the document changes OR when the background parser advances
 * (the language tree reference changes between transactions). The tree check is
 * essential: CodeMirror parses incrementally, so a block node (table / mermaid
 * fence) below the initial parse budget enters the tree later via a parse
 * transaction that carries NO doc change. A `docChanged`-only field computes
 * once against the partial tree and never sees those nodes — leaving real-world
 * docs (a table 40 lines down) stuck on raw markdown until the next edit.
 */
function syntaxDecorationField(build: (state: EditorState) => DecorationSet): StateField<DecorationSet> {
	return StateField.define<DecorationSet>({
		create: build,
		update(deco, tr) {
			if (tr.docChanged || syntaxTree(tr.state) !== syntaxTree(tr.startState)) return build(tr.state);
			return deco.map(tr.changes);
		},
		provide: (f) => EditorView.decorations.from(f)
	});
}

const markdownTableDecorations = syntaxDecorationField(buildTableDecorations);

/**
 * Replaces each ```mermaid fenced block with a rendered diagram block widget.
 * Mirrors the table field: block decorations must come from a state field, and
 * the replaced range spans whole lines so the widget cleanly takes the block's
 * place.
 */
function buildMermaidDecorations(state: EditorState): DecorationSet {
	const decos: Range<Decoration>[] = [];
	const { doc } = state;
	syntaxTree(state).iterate({
		enter: (node) => {
			if (node.name !== 'FencedCode') return;
			const code = mermaidFence(node, doc);
			if (code === null) return; // a non-mermaid fence: left as styled code
			const from = doc.lineAt(node.from).from;
			const to = doc.lineAt(Math.min(node.to, doc.length)).to;
			decos.push(Decoration.replace({ widget: new MermaidWidget(code), block: true }).range(from, to));
			return false;
		}
	});
	return Decoration.set(decos, true);
}

const markdownMermaidDecorations = syntaxDecorationField(buildMermaidDecorations);

export const markdownLivePreview: Extension = [
	markdownInlinePreview,
	markdownTableDecorations,
	markdownMermaidDecorations,
	markdownImages,
	selectImageOnClick,
	openSelectedImageOnEnter,
	openLinkOnClick,
	openLinkOnAltEnter
];
