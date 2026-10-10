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
