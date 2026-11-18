import { Transaction } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { scrollToRange, type ScrollOrigin } from './comment-marks';
import { revealTypedUnder } from './typed-reveal';

export type JumpTarget = { from: number; to: number; focus?: boolean; origin: ScrollOrigin };

/** Bring a comment's text into view; with `focus`, put the caret at its start and focus the text (the host on a touch screen, so no keyboard opens). */
export function jumpToComment(editor: EditorView, target: JumpTarget, host: HTMLElement | undefined): void {
	revealTypedUnder(editor, [target]);
	scrollToRange(editor, target.from, target.to, target.origin);
	if (!target.focus) return;
	editor.dispatch({ selection: { anchor: Math.min(target.from, editor.state.doc.length) }, annotations: Transaction.userEvent.of('select.doc-comment') });
	if (window.matchMedia?.('(pointer: coarse)').matches) host?.focus({ preventScroll: true });
	else editor.focus();
}
