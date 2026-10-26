import type { UpdateCard } from '$lib/api/threadUpdates';
import { sourceRange } from './contextEventModel';
import type { TranscriptEntry } from './transcriptFocusModel';

/** Outcomes that ask nothing of the user: finished, closed, or failed on an item the user does not own. */
export function isPassive(card: UpdateCard): boolean {
	return card.kind === 'done' || card.kind === 'done_follow_up' || card.kind === 'closed'
		|| (card.kind === 'failed' && !card.asks_user);
}

/**
 * B4, B5: a card that asks something of you stands on its own and closes the block it
 * was recorded in.
 */
export function asksYou(card: UpdateCard): boolean {
	return (card.kind === 'failed' && card.asks_user)
		|| (card.kind === 'blocked' && card.blocked_asks_user)
		// A person step stands on its own in any state: open it is an ask, settled it is the receipt of one.
		|| card.kind === 'person_step';
}

/** A person step the server still read as waiting on you. */
export function isReadyStep(card: UpdateCard): boolean {
	return card.kind === 'person_step' && (card.step_state ?? 'open') === 'open';
}

/** B6, B22: the card asks nothing and is not a completion: a line inside its block. */
export function isBlockLine(card: UpdateCard): boolean {
	return !isPassive(card) && !asksYou(card);
}

function precedes(card: UpdateCard, boundary: UpdateCard): boolean {
	return card.after_seq < boundary.after_seq || (card.after_seq === boundary.after_seq && card.id < boundary.id);
}

/** Sorted cards advance once. Retained active-card fences flush a group; work never does. */
export function placePassive(entries: TranscriptEntry[], cards: UpdateCard[]): TranscriptEntry[] {
	const placed: TranscriptEntry[] = [];
	let next = 0;
	const flush = (belongsBefore: (card: UpdateCard) => boolean) => {
		const group: UpdateCard[] = [];
		while (next < cards.length && belongsBefore(cards[next])) group.push(cards[next++]);
		if (group.length) placed.push({ kind: 'passive', id: `passive-update-${group[0].id}`, cards: group });
	};
	for (const entry of entries) {
		if (entry.kind === 'message') {
			const min = sourceRange(entry.message)?.min ?? Infinity;
			flush((card) => card.after_seq < min);
		} else if (entry.kind === 'update') {
			flush((card) => precedes(card, entry.card));
		} else if (entry.kind === 'pause') {
			flush((card) => card.after_seq <= (entry.after_seq ?? entry.pause.source.seq));
		} else if (entry.kind === 'stub-pause') {
			flush((card) => card.after_seq <= entry.after_seq);
		}
		placed.push(entry);
	}
	flush(() => true);
	return placed;
}
