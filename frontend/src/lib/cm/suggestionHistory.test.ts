import { describe, expect, it } from 'vitest';
import { EditorState, Transaction } from '@codemirror/state';
import { history, redo, undo } from '@codemirror/commands';
import { acceptedSuggestion, suggestionHistory, suggestionHistoryEffect } from './suggestionHistory';

function capture(state: EditorState, command: typeof undo) {
	let pending: Transaction | undefined;
	const handled = command({ state, dispatch: tr => { pending = tr; } });
	return { handled, transaction: pending };
}

describe('suggestion history primitive', () => {
	it('stages one accept for undo and redo without advancing history on refusal', () => {
		let state = EditorState.create({ doc: 'old', extensions: [history(), suggestionHistory] });
		state = state.update({ changes: { from: 0, to: 3, insert: 'new' }, ...acceptedSuggestion('event-1', ['s1']) }).state;
		const held = capture(state, undo);
		expect(held.handled).toBe(true);
		expect(held.transaction?.newDoc.toString()).toBe('old');
		expect(held.transaction?.effects.find(e => e.is(suggestionHistoryEffect))?.value).toEqual({ eventId: 'event-1', ids: ['s1'], direction: 'undo' });
		// A refused server decision leaves the original state and history untouched.
		expect(capture(state, undo).transaction?.newDoc.toString()).toBe('old');
		state = held.transaction!.state;
		const forward = capture(state, redo);
		expect(forward.transaction?.newDoc.toString()).toBe('new');
		expect(forward.transaction?.effects.find(e => e.is(suggestionHistoryEffect))?.value).toEqual({ eventId: 'event-1', ids: ['s1'], direction: 'redo' });
	});

	it('keeps typing and grouped accepts in order and clears redo after a new edit', () => {
		let state = EditorState.create({ doc: 'a', extensions: [history(), suggestionHistory] });
		state = state.update({ changes: { from: 0, to: 1, insert: 'b' }, ...acceptedSuggestion('first', ['s1']) }).state;
		state = state.update({ changes: { from: 0, to: 1, insert: 'c' }, ...acceptedSuggestion('second', ['s2', 's3']) }).state;
		state = state.update({ changes: { from: 1, insert: '!' }, userEvent: 'input.type' }).state;
		let next = capture(state, undo).transaction!;
		expect(next.newDoc.toString()).toBe('c');
		expect(next.effects.some(e => e.is(suggestionHistoryEffect))).toBe(false);
		state = next.state;
		next = capture(state, undo).transaction!;
		expect(next.newDoc.toString()).toBe('b');
		expect(next.effects.find(e => e.is(suggestionHistoryEffect))?.value).toEqual({ eventId: 'second', ids: ['s2', 's3'], direction: 'undo' });
		state = next.state;
		next = capture(state, undo).transaction!;
		expect(next.newDoc.toString()).toBe('a');
		expect(next.effects.find(e => e.is(suggestionHistoryEffect))?.value.eventId).toBe('first');
		state = next.state;
		state = capture(state, redo).transaction!.state;
		expect(state.doc.toString()).toBe('b');
		state = state.update({ changes: { from: 1, insert: '?' }, userEvent: 'input.type' }).state;
		expect(capture(state, redo).handled).toBe(false);
	});
});
