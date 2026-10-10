import type { UpdateCard } from '$lib/api/threadUpdates';
import { parseWsTimestamp } from '$lib/time';
import { passiveContextEventLabel, sourceRange } from './contextEventModel';
import type { TranscriptMessage } from './messageListModel';
import {
	rowKind,
	type TranscriptFoldEntry,
	type TranscriptMessageEntry,
} from './transcriptFocusModel';

/**
 * What a work block holds, in order, once it is open.
 *
 * `blockEntries(run)` is the one place a block's rows, the plan cards recorded
 * inside it and the visit divider become one list. The surface renders each entry
 * by `kind`; a `step` is a row exactly as the block shows it today, a `line` is
 * one quiet line of text with its time, and a `divider` is "Since your last visit".
 * Every `line` carries the row it came from (`message`) or the card (`card`), so
 * the surface may draw a richer control for it; the text inputs below are what
 * the line says.
 */
export type BlockLine =
	/** B17: `Moved to Claude Code · FOMRRT3 · account limit · FOMRRT2 resets 2:00 AM`. */
	| {
		type: 'account_move';
		/** The receipt's own words: `Moved to <provider> · <account>`. */
		destination: string;
		/** `account limit` when the move was caused by a limit; null otherwise. */
		reason: string | null;
		/** Name of the account moved from, when stored. */
		fromAccount: string | null;
		/** When the limited account resets (ISO), only for an account-limit move. */
		resetsAt: string | null;
		/** `resets 2:00 AM`, or `resets Oct 10, 2:00 AM` when not today; null without a reset. */
		reset: string | null;
		text: string;
	}
	| { type: 'context_update'; text: string }
	| { type: 'model_switch'; text: string }
	| { type: 'subagent_finished'; text: string }
	/** B15b: `From <agent>: <first line>`. */
	| { type: 'agent_message'; from: string; text: string }
	/** B12-B14, recovered: red `Error: <message>`. */
	| { type: 'error_recovered'; message: string; text: string }
	/** B20: the provider's result text, when it has any. */
	| { type: 'result'; text: string }
	/** B6, B22: `<title> · Done|Closed|Blocked|Failed`. */
	| { type: 'plan_card'; title: string; state: UpdateCard['kind']; outcome: string; text: string };

export type BlockEntry =
	| { kind: 'step'; id: string; item: TranscriptMessageEntry }
	| {
		kind: 'line'; id: string; line: BlockLine;
		/** When it happened (server stamp); the surface formats it by the transcript's time rule. */
		at: string | null;
		message?: TranscriptMessage;
		card?: UpdateCard;
	}
	| { kind: 'divider'; id: string; truncated: boolean };

const OUTCOMES: Record<UpdateCard['kind'], string> = {
	done: 'Done',
	done_follow_up: 'Done, follow-up added',
	failed: 'Failed',
	closed: 'Closed',
	blocked: 'Blocked',
};

function firstLine(text: unknown): string {
	return String(text ?? '').split('\n').find((line) => line.trim())?.trim() ?? '';
}

function clock(at: Date): string {
	return at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

/** A reset time with a date when it is not today, so it never reads as already past. */
export function formatResetTime(iso: string, now: Date = new Date()): string | null {
	const at = new Date(parseWsTimestamp(iso));
	if (Number.isNaN(at.getTime())) return null;
	if (at.toDateString() === now.toDateString()) return clock(at);
	const year = at.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' as const };
	return `${at.toLocaleDateString([], { month: 'short', day: 'numeric', ...year })}, ${clock(at)}`;
}

function accountMove(message: TranscriptMessage, now: Date): BlockLine {
	const meta = message.metadata ?? {};
	const destination = message.content || `Moved to ${meta.to_account_name ?? 'another account'}`;
	const limited = meta.reason === 'account_limit';
	const from = typeof meta.from_account_name === 'string' && meta.from_account_name ? meta.from_account_name : null;
	const resetsAt = limited && typeof meta.resets_at === 'string' ? meta.resets_at : null;
	const when = resetsAt ? formatResetTime(resetsAt, now) : null;
	const reset = when ? `resets ${when}` : null;
	const parts = [destination, limited ? 'account limit' : null, reset ? `${from ?? 'Source'} ${reset}` : null];
	return {
		type: 'account_move', destination, reason: limited ? 'account limit' : null, fromAccount: from,
		resetsAt, reset, text: parts.filter(Boolean).join(' · '),
	};
}

/** The line a non-visible row becomes inside a block, or null when it stays a step row (or shows nothing). */
function lineOf(message: TranscriptMessage, now: Date): BlockLine | 'nothing' | null {
	const kind = rowKind(message);
	if (kind === 'account_move') return accountMove(message, now);
	if (kind === 'context_update') return { type: 'context_update', text: passiveContextEventLabel(message) };
	if (kind === 'model_switch') return { type: 'model_switch', text: firstLine(message.content) };
	if (kind === 'subagent_finished') return { type: 'subagent_finished', text: firstLine(message.content) };
	if (kind === 'agent_message') {
		const from = String(message.metadata?.from_title || 'agent');
		return { type: 'agent_message', from, text: `From ${from}: ${firstLine(message.content)}` };
	}
	if (kind === 'error_recovered') {
		const body = firstLine(message.content);
		return { type: 'error_recovered', message: body, text: /^error\b/i.test(body) ? body : `Error: ${body}` };
	}
	if (message.role === 'system' && message.metadata?.type === 'result') {
		const text = firstLine(message.content);
		return text ? { type: 'result', text } : 'nothing';
	}
	return null;
}

export function cardLine(card: UpdateCard): BlockLine {
	const title = card.title || 'Plan item';
	const outcome = OUTCOMES[card.kind] ?? card.kind;
	return { type: 'plan_card', title, state: card.kind, outcome, text: `${title} · ${outcome}` };
}

function positionOf(item: TranscriptMessageEntry): { min: number; max: number } {
	const range = sourceRange(item.message);
	if (range) return range;
	// Not sequenced yet: at the live edge, after every card recorded so far.
	return { min: Infinity, max: Infinity };
}

export type BlockEntryOptions = {
	/**
	 * Whether the block's rows are read back to its start. A stub-backed block reads
	 * newest rows first, so the divider is only placed above a loaded row when an
	 * older loaded row proves it is not the first one. Default: true without a stub.
	 */
	complete?: boolean;
	now?: Date;
};

/** The block's rows, cards and divider as one ordered list. */
export function blockEntries(run: TranscriptFoldEntry, options: BlockEntryOptions = {}): BlockEntry[] {
	const now = options.now ?? new Date();
	const complete = options.complete ?? !run.stub;
	const cards = [...run.cards ?? []].sort((a, b) => a.after_seq - b.after_seq || a.id - b.id);
	const firstSeq = run.divider?.first_seq;
	const entries: BlockEntry[] = [];
	let nextCard = 0;
	let dividerDone = !run.divider;
	const dividerEntry = (): BlockEntry => ({ kind: 'divider', id: `${run.id}:visit-divider`, truncated: run.divider?.truncated === true });
	const pushCard = (card: UpdateCard) => {
		if (!dividerDone && run.divider?.beforeCardId === card.id && (complete || entries.length > 0)) {
			entries.push(dividerEntry());
			dividerDone = true;
		}
		entries.push({ kind: 'line', id: `card-${card.id}`, line: cardLine(card), at: card.created_at, card });
	};
	run.items.forEach((item, index) => {
		const position = positionOf(item);
		while (nextCard < cards.length && cards[nextCard].after_seq < position.min) pushCard(cards[nextCard++]);
		if (!dividerDone && typeof firstSeq === 'number' && position.min >= firstSeq && (complete || entries.length > 0 || index > 0)) {
			entries.push(dividerEntry());
			dividerDone = true;
		}
		const line = lineOf(item.message, now);
		if (line === 'nothing') return;
		if (line === null) {
			entries.push({ kind: 'step', id: item.id, item });
			return;
		}
		entries.push({ kind: 'line', id: item.id, line, at: item.message.created_at ?? null, message: item.message });
	});
	while (nextCard < cards.length) pushCard(cards[nextCard++]);
	return entries;
}
