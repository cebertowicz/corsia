import type { ThreadPlanStateEntry } from '$lib/api/plans';
import { attentionKindWord, chatStateWord } from '$lib/components/plans/planDisplay';

/** Whether the row's plan state is something the user owes. */
export function isYourMove(entry: ThreadPlanStateEntry | undefined): boolean {
	return entry?.state === 'needs_input' || entry?.state === 'blocked_on_you';
}

/**
 * The word slot 3 shows by precedence your move > Failed > Stuck (AS3);
 * empty when neither applies, which leaves slot 3 to the update dot.
 */
export function slotThreeWord(entry: ThreadPlanStateEntry | undefined): string {
	if (!entry) return '';
	const items = entry.attention_items ?? [];
	const count = items.length;
	if (entry.state === 'failed') return count > 1 ? `${count} ${attentionKindWord('failed')}` : attentionKindWord('failed');
	if (entry.state === 'stuck') {
		const overdue = entry.stuck_overdue_waits ?? 0;
		return overdue > 1 ? `${overdue} Stuck` : 'Stuck';
	}
	if (!isYourMove(entry)) return '';
	const kind = items[0]?.kind;
	if (count === 0) return chatStateWord(entry.state);
	if (count === 1 && kind) return attentionKindWord(kind);
	if (items.every((item) => item.kind === 'decision')) return `${count} to decide`;
	return `${count} for you`;
}

/**
 * Mark read/unread moves only the update dot, so the menu offers it only when
 * the dot is slot 3's mark: the thread has a seen record and no owed or
 * Failed or Stuck word outranks the dot.
 */
export function canMarkSeen(entry: ThreadPlanStateEntry | undefined): boolean {
	return Boolean(entry?.opened) && slotThreeWord(entry) === '';
}
