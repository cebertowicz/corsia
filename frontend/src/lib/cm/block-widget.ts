import { EditorView, WidgetType, type DecorationSet } from '@codemirror/view';
import { syntaxTree } from '@codemirror/language';
import { StateField, type EditorState, type Transaction } from '@codemirror/state';

/**
 * A block widget that starts at the height its predecessor was drawn at. An edit to a table or diagram replaces its widget,
 * and CodeMirror sizes a new block widget at one line until it is measured: a tall table shrunk to one line takes the text
 * below it up past the reader, drops out of the drawn area and is never measured, so the reader is left on another block.
 */
export abstract class BlockWidget extends WidgetType {
	private drawn: HTMLElement | null = null;
	private prior: BlockWidget | null;
	constructor(prior: BlockWidget | null) {
		super();
		// Keep only a predecessor that was drawn, so the chain never grows while a block sits off screen.
		this.prior = prior?.drawn ? prior : prior?.prior ?? null;
	}
	get estimatedHeight(): number {
		// A view that is not laid out (hidden, detached) draws at zero, which is no estimate either.
		return (this.drawn?.isConnected && this.drawn.offsetHeight) || (this.prior?.estimatedHeight ?? -1);
	}
	protected drew(el: HTMLElement) {
		this.drawn = el;
		this.prior = null;
		return el;
	}
}

/** The block widget of `type` the previous decoration set had over `from`–`to` (mapped through the change), if any. */
export function priorWidget<T extends BlockWidget>(prior: DecorationSet | undefined, from: number, to: number, type: abstract new (...args: never[]) => T): T | null {
	let found: T | null = null;
	prior?.between(from, to, (_from, _to, deco) => {
		if (!(deco.spec.widget instanceof type)) return;
		found = deco.spec.widget;
		return false;
	});
	return found;
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
 * `alsoOn` names any other transaction the field rebuilds on (the table's: an image refresh).
 */
export function syntaxDecorationField(
	build: (state: EditorState, prior?: DecorationSet) => DecorationSet,
	alsoOn: (tr: Transaction) => boolean = () => false
): StateField<DecorationSet> {
	return StateField.define<DecorationSet>({
		create: (state) => build(state),
		update(deco, tr) {
			const mapped = deco.map(tr.changes);
			if (tr.docChanged || alsoOn(tr) || syntaxTree(tr.state) !== syntaxTree(tr.startState)) return build(tr.state, mapped);
			return mapped;
		},
		provide: (f) => EditorView.decorations.from(f)
	});
}
