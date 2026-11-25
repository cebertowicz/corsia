import { history } from '@codemirror/commands';
import type { Compartment } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { acceptedSuggestion } from './suggestionHistory';

/** Replace only the changed span so marks on untouched text retain their positions. */
export function changedSpan(current: string, next: string) {
	let from = 0;
	while (from < current.length && from < next.length && current[from] === next[from]) from += 1;
	let tail = 0;
	while (tail < current.length - from && tail < next.length - from && current[current.length - 1 - tail] === next[next.length - 1 - tail]) tail += 1;
	return { from, to: current.length - tail, insert: next.slice(from, next.length - tail) };
}

/** A reload replaces the document's basis; old undo steps cannot safely apply to it. */
export function replaceExternalDocument(view: EditorView, next: string, historyComp: Compartment): void {
	view.dispatch({ changes: changedSpan(view.state.doc.toString(), next), effects: historyComp.reconfigure([]) });
	view.dispatch({ effects: historyComp.reconfigure(history()) });
}

/** Record the accepted text and receipt as one isolated local history step. */
export function acceptSuggestion(view: EditorView, text: string, eventId: string, ids: string[]): boolean {
	view.dispatch({ changes: changedSpan(view.state.doc.toString(), text), ...acceptedSuggestion(eventId, ids) });
	return true;
}
