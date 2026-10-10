import type { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { ensureSyntaxTree, syntaxTree } from '@codemirror/language';

/** Source-line anchors handle duplicate headings and omit headings inside code fences. */
export function nearestHeadingSourceLine(state: EditorState, top: number): number | null {
	const tree = ensureSyntaxTree(state, top, 50) ?? syntaxTree(state);
	let line: number | null = null;
	tree.iterate({
		to: top,
		enter(node) {
			if (node.from <= top && /^(ATXHeading|SetextHeading)\d$/.test(node.name)) {
				line = state.doc.lineAt(node.from).number;
			}
		},
	});
	return line;
}

/**
 * The longest heading line sent for the server to compare with the saved file.
 * The server compares at most this many characters of each line, so a line goes
 * whole or not at all: a longer heading is not named and the note carries the path.
 * Keep equal to `_SHOWN_LINE_CAP` in backend/infra/document_viewing.py.
 */
const HEADING_LINE_SENT = 200;

export interface ViewingHeading {
	/** One-based source line of the heading. */
	anchor: number;
	/** That line as the editor shows it; the server compares it and never stores it. */
	line: string;
}

/** Read the mounted editor at send time, without updating the editor or sending scroll events. */
export function viewingHeading(root: HTMLElement | undefined): ViewingHeading | null {
	const editor = root?.querySelector<HTMLElement>('.cm-editor');
	const view = editor ? EditorView.findFromDOM(editor) : null;
	if (!view) return null;
	const top = view.lineBlockAtHeight(view.scrollDOM.scrollTop).from;
	const anchor = nearestHeadingSourceLine(view.state, top);
	if (anchor == null) return null;
	const line = view.state.doc.line(anchor).text;
	return line.length > HEADING_LINE_SENT ? null : { anchor, line };
}
