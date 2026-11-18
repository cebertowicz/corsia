import type { EditorView } from '@codemirror/view';

/** The frame places are measured from: the document pane, or the scroller for an editor outside one. */
const frameOf = (view: EditorView): HTMLElement => view.dom.closest<HTMLElement>('.doc') ?? view.scrollDOM;
/** The edge places are measured from: the frame's top on screen. */
export const frameTop = (view: EditorView): number => frameOf(view).getBoundingClientRect().top;

/** The frame's layout top on the page, which no scroll (a phone keyboard showing the caret) or transform moves. */
export function frameLayoutTop(view: EditorView): number {
	let top = 0;
	for (let el: HTMLElement | null = frameOf(view); el; el = el.offsetParent as HTMLElement | null) top += el.offsetTop;
	return top;
}
