import { WidgetType, type DecorationSet } from '@codemirror/view';

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
