import { StateEffect } from '@codemirror/state';
import { invertedEffects, isolateHistory } from '@codemirror/commands';

/** One server decision, including every suggestion actually accepted in a batch. */
export type SuggestionHistoryEntry = { eventId: string; ids: readonly string[]; direction: 'undo' | 'redo' };

export const suggestionHistoryEffect = StateEffect.define<SuggestionHistoryEntry>();

/** CodeMirror stores the inverse effect alongside the inverse text change. */
export const suggestionHistory = invertedEffects.of(transaction => transaction.effects.flatMap(effect => {
	if (!effect.is(suggestionHistoryEffect)) return [];
	return [suggestionHistoryEffect.of({ ...effect.value, direction: effect.value.direction === 'undo' ? 'redo' : 'undo' })];
}));

/** A successful acceptance is a distinct step, never merged with typing on either side. */
export function acceptedSuggestion(eventId: string, ids: readonly string[]) {
	return {
		effects: suggestionHistoryEffect.of({ eventId, ids, direction: 'redo' }),
		annotations: isolateHistory.of('full'),
	};
}
