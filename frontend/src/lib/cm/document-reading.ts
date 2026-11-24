import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { captureDocumentPlace, dispatchKeepingPlace, holdPlace } from '$lib/documentReadingPlace';
import { closeCell } from './widget-cell-state';
import { widgetOf } from './widget-blocks';
import { keepingSelection } from './widget-paint';

/** Approach A: noneditable DOM remains focusable, paired with the editor's input contract. */
export function documentInputPolicy(readOnly: boolean, reading: boolean) {
	return [EditorState.readOnly.of(readOnly || reading), EditorView.editable.of(!reading),
		EditorView.contentAttributes.of(reading ? { tabindex: '0' } : {})];
}

/** Preserve a native widget range even while focused CodeMirror synchronizes its caret. */
export function dispatchReadingTransactions(...[transactions, view]: Parameters<typeof dispatchKeepingPlace>) {
	const selection = document.getSelection();
	const widget = !view.state.facet(EditorView.editable) && selection?.anchorNode
		? widgetOf(view, selection.anchorNode) : null;
	if (widget) keepingSelection(widget, () => dispatchKeepingPlace(transactions, view));
	else dispatchKeepingPlace(transactions, view);
}

/** Mode changes end document/cell typing, never focus an editor or touch a comment's field. */
export function clearDocumentInput(view: EditorView) {
	const place = captureDocumentPlace(view);
	const focused = document.activeElement;
	if (focused instanceof HTMLElement && view.dom.contains(focused)) focused.blur();
	const selection = document.getSelection();
	if (selection?.anchorNode && view.dom.contains(selection.anchorNode)) selection.removeAllRanges();
	view.dispatch({ selection: { anchor: view.state.selection.main.head }, effects: closeCell.of(null) });
	// The collapsed source caret is not a browser selection waiting for the next Edit.
	if (selection?.anchorNode && view.dom.contains(selection.anchorNode)) selection.removeAllRanges();
	holdPlace(view, place);
}
