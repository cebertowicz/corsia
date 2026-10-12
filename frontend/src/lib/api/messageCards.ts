/** One artifact card a message or a blocked update row carries, computed by the server when the page is read. */
export interface MessageCard {
	/** An ask is a user ask the agent raised (it has a round state); an artifact is a file the message names. */
	kind: 'ask' | 'artifact';
	artifact_id: string;
	title: string;
	type: string | null;
	icon: string;
	name: string | null;
	path: string | null;
	available: boolean;
	/** The artifact pane can open it in the canvas. */
	opens: boolean;
	hold_id?: string;
	/** A reference card for a file an ask in this Turn covers: the holds whose ask cards cover it. */
	covered_by_ask?: string[];
	state?: MessageCardState;
	feedback?: { comments?: number; suggestions?: number; edits?: number };
}

export type MessageCardState =
	| 'needs_review' | 'approved' | 'approved_with_changes' | 'changes_requested'
	| 'answered' | 'replaced' | 'unavailable';

/** Neutral state labels: true for any reader, short enough not to wrap at 393. */
export const CARD_STATE_LABELS: Record<MessageCardState, string> = {
	needs_review: 'Needs your review',
	approved: 'Approved',
	approved_with_changes: 'Approved with changes',
	changes_requested: 'Changes requested',
	answered: 'Answered',
	replaced: 'Replaced',
	unavailable: 'No longer available',
};

/** "2 comments, 1 suggestion": the round named by kind, leaving out kinds with no items. */
export function feedbackLine(feedback: MessageCard['feedback']): string | null {
	const part = (count: number | undefined, one: string) =>
		count && count > 0 ? `${count} ${one}${count === 1 ? '' : 's'}` : null;
	const parts = [
		part(feedback?.comments, 'comment'), part(feedback?.suggestions, 'suggestion'), part(feedback?.edits, 'edit'),
	].filter((value): value is string => value !== null);
	return parts.length ? parts.join(', ') : null;
}

/**
 * The cards of one message that show. An ask card whose blocked update row is on screen shows
 * there, not here. A reference card covered by asks shows only when none of those ask cards is on
 * screen (its host row is collapsed or on another page), so the reviewed document is visible in
 * both Focus modes and never twice. `rendered` is the holds the transcript shows elsewhere.
 */
export function visibleCards(
	cards: readonly MessageCard[], homed?: ReadonlySet<string>, rendered?: ReadonlySet<string>,
): MessageCard[] {
	const here = cards.filter((card) => !(card.hold_id && homed?.has(card.hold_id)));
	const onScreen = new Set([...rendered ?? [], ...homed ?? [], ...here.flatMap((card) => card.hold_id ?? [])]);
	return here.filter((card) => !card.covered_by_ask?.some((hold) => onScreen.has(hold)));
}
