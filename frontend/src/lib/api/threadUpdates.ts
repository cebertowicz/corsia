import type { MessageCard } from './messageCards';

/** A plan update card: the item's title as it read when the outcome happened, and that outcome (AS17). */
export interface UpdateCard {
	id: number;
	thread_id: string;
	kind: 'done' | 'done_follow_up' | 'failed' | 'closed' | 'blocked' | 'person_step';
	node_id: string | null;
	title: string | null;
	/** The transcript message the card follows; 0 precedes the first message. */
	after_seq: number;
	created_at: string;
	/** The plan item still exists, so its title may open it. */
	node_exists: boolean;
	/** The plan item is assigned to the user now; only then does a failure ask for them. */
	asks_user: boolean;
	/** The plan item is blocked on the user now (assigned to them, or holding their ask), in work and in a live branch: a block asks for them (B5). */
	blocked_asks_user: boolean;
	/** The user ask this blocked update recorded, as it reads now; null when it has none or its hold is gone. */
	ask?: MessageCard | null;
	/** A person step's card: how the step stood when the thread was read. The plan owns the live state. */
	step_state?: 'open' | 'done' | 'closed';
}

/** Where "Since your last visit" goes; `first_seq` is null when only cards are new. */
export interface VisitDivider {
	since: string;
	first_seq: number | null;
	/** Older unseen messages precede the bounded transcript scan. */
	truncated?: true;
}

export interface UpdateCardsPage {
	cards: UpdateCard[];
	/** True only when the fixed newest window omits older cards. */
	has_more: boolean;
	divider: VisitDivider | null;
}

/** The newest bounded cards from `fromSeq`, returned in transcript order. Null when the read failed. */
export async function readUpdateCards(
	bearerValue: string,
	threadId: string,
	options: { fromSeq: number; divider?: boolean },
): Promise<UpdateCardsPage | null> {
	const params = new URLSearchParams({ from_seq: String(options.fromSeq) });
	if (options.divider) params.set('divider', 'true');
	try {
		const res = await fetch(`/api/threads/${encodeURIComponent(threadId)}/updates?${params}`, {
			headers: { Authorization: `Bearer ${bearerValue}` },
		});
		if (!res.ok) {
			console.error('[thread updates] read rejected', threadId, res.status);
			return null;
		}
		const body = (await res.json()) as Partial<UpdateCardsPage> | null;
		if (!Array.isArray(body?.cards)) {
			console.error('[thread updates] malformed response', threadId);
			return null;
		}
		return { cards: body.cards, has_more: body.has_more === true, divider: body.divider ?? null };
	} catch (error) {
		console.error('[thread updates] read failed', threadId, error);
		return null;
	}
}
