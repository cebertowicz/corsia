import { Compartment, EditorState, Prec, type Transaction } from '@codemirror/state';
import { historyKeymap, redo, undo } from '@codemirror/commands';
import { EditorView, keymap } from '@codemirror/view';
import { suggestionHistoryEffect, type SuggestionHistoryEntry } from './suggestionHistory';

type Transition = (entry: SuggestionHistoryEntry, before: string, after: string) => Promise<boolean>;

/** Hold a suggestion undo until its document and decision have both been written. */
export function suggestionHistoryKeys(transition: Transition, recover: () => void) {
	const locked = new Compartment();
	let busy = false;
	function capture(view: EditorView, direction: 'undo' | 'redo'): Transaction | undefined {
		let transaction: Transaction | undefined;
		(direction === 'undo' ? undo : redo)({ state: view.state, dispatch: value => { transaction = value; } });
		return transaction;
	}
	function run(view: EditorView, direction: 'undo' | 'redo'): boolean {
		if (busy) return true;
		const pending = capture(view, direction);
		if (!pending) return false;
		const entry = pending.effects.find(effect => effect.is(suggestionHistoryEffect))?.value;
		if (!entry) { view.dispatch(pending); return true; }
		const before = view.state.doc.toString(), after = pending.newDoc.toString();
		busy = true;
		view.dispatch({ effects: locked.reconfigure(EditorState.readOnly.of(true)) });
		void (async () => {
			try {
				if (!await transition(entry, before, after) || !view.dom.isConnected) return;
				view.dispatch({ effects: locked.reconfigure([]) });
				const next = capture(view, direction);
				const nextEntry = next?.effects.find(effect => effect.is(suggestionHistoryEffect))?.value;
				if (view.state.doc.toString() !== before || next?.newDoc.toString() !== after || nextEntry?.eventId !== entry.eventId) {
					recover();
					return;
				}
				view.dispatch(next);
			} catch {
				if (view.dom.isConnected) recover();
			} finally {
				if (view.dom.isConnected) view.dispatch({ effects: locked.reconfigure([]) });
				busy = false;
			}
		})();
		return true;
	}
	const keys = historyKeymap.filter(binding => binding.run === undo || binding.run === redo).map(binding => ({
		...binding,
		run: (view: EditorView) => run(view, binding.run === undo ? 'undo' : 'redo'),
	}));
	return [locked.of([]), Prec.highest(keymap.of(keys))];
}
