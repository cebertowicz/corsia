import type { UpdateCard } from '$lib/api/threadUpdates';
import { parseBrowserChip, type BrowserChip } from '$lib/browserChip';
import type { TranscriptMessage } from './messageListModel';
import { SOURCE_MEMBERS, sourceMembers, sourceRange } from './contextEventModel';
import type { PlanLimitPauseDescriptor } from './planLimitPause';
import { combineStubs, stubTokens, type RunStub } from '$lib/focusRuns';
import { hasAssistantActivity, isHumanMessage, isSystemNotice } from './transcriptRowKinds';

export { isFocusVisible, isRecoveredError, rowKind, runKinds, type RowKind } from './transcriptRowKinds';

export type PauseAnnotation = { pause: PlanLimitPauseDescriptor; showProxy: boolean };
export type TranscriptPauseEntry = PauseAnnotation & { kind: 'pause'; id: string; /** Whole opaque stub bound, separate from raw source. */ after_seq?: number };
/** A canonical stopped Turn in an opaque stub; only the whole stub's end is known. */
export type TranscriptStubPauseEntry = { kind: 'stub-pause'; id: string; turnId: string; after_seq: number };

export type TranscriptMessageEntry = {
	kind: 'message';
	id: string;
	message: TranscriptMessage;
	showActivity: boolean;
	showContent: boolean;
};

/** The visit divider drawn inside the block that holds the first new thing (F2b, F3). */
export type BlockDivider = Pick<TranscriptDividerEntry, 'truncated' | 'first_seq'> & {
	/** Set when the first new thing is a card line: the line goes before that card. */
	beforeCardId?: number;
};

/** A block: the work and bookkeeping between two things you must read or act on. */
export type TranscriptFoldEntry = {
	kind: 'fold';
	/** List key only (`run:<closing item>` or the open key); expansion never reads it. */
	id: string;
	items: TranscriptMessageEntry[];
	/** Distinct non-null Turn ids in transcript order. */
	turnIds: string[];
	/** Only the trailing open run can hold the currently running Turn. */
	live: boolean;
	/** Rows a Focus page left unloaded; the run's rows read from its range when it is expanded. */
	stub?: RunStub;
	/** Source slots, never work items, weights, expansion identities or Turn boundaries. */
	pauses?: (PauseAnnotation & { afterItem: number })[];
	/**
	 * Plan cards recorded inside this block, shown as lines: an agent's own blocked task
	 * (B6) and any card that asks nothing of you and is not in a completion list (B22).
	 * `blockEntries` places each at its position among the rows.
	 */
	cards?: UpdateCard[];
	/** F2a: the first new row of the visit is inside this block, known from its range alone. */
	new?: boolean;
	/** F2b: where the divider goes once the block's rows load; only on the block that is `new` because of it. */
	divider?: BlockDivider;
	/** The window the block's newest open put in front of the person; the collapsed block shows it beside its capsule. */
	browserChip?: BrowserChip;
};

/** List key of the one run nothing has closed yet. */
export const OPEN_RUN_KEY = 'run:open';

/** A plan update card; `earlier` counts older cards on the same item it stands for. */
export type TranscriptUpdateEntry = {
	kind: 'update';
	id: string;
	card: UpdateCard;
	earlier: number;
};

/** Retained non-attention lifecycle outcomes, presented after their work gap. */
export type TranscriptPassiveEntry = {
	kind: 'passive';
	id: string;
	cards: UpdateCard[];
};

export type TranscriptDividerEntry = {
	kind: 'divider'; id: string; truncated?: boolean;
	/** Existing visit boundary; missing/null remains unavailable for source placement. */
	first_seq?: number | null;
};

export type TranscriptEntry =
	| TranscriptMessageEntry
	| TranscriptFoldEntry
	| TranscriptUpdateEntry
	| TranscriptPassiveEntry
	| TranscriptDividerEntry
	| TranscriptPauseEntry
	| TranscriptStubPauseEntry;

/**
 * Entries that give a person something to read or act on in Focus mode. A block
 * counts as one: a page whose only content is a block has something to show, so
 * it never pages older history on its own.
 */
export function countVisibleEntries(entries: TranscriptEntry[]): number {
	return entries.reduce((count, entry) => count + (entry.kind === 'passive'
		? entry.cards.length
		: Number((entry.kind === 'message' && entry.showContent) || entry.kind === 'update' || entry.kind === 'fold')), 0);
}

export function visibleEntryContinuation(entries: () => TranscriptEntry[]): () => boolean {
	const initialCount = countVisibleEntries(entries());
	return () => countVisibleEntries(entries()) <= initialCount;
}

// The owning agent still receives this receipt; only Focus mode hides it from the human view.
function isAgentOnlyReceipt(message: TranscriptMessage): boolean {
	return message.role === 'system'
		&& message.metadata?.type === 'activity_receipt'
		&& message.metadata?.event_type === 'usage.recording_failed';
}

/** A run of these items under a list key. */
export function runEntry(
	items: TranscriptMessageEntry[],
	id: string,
	live = false,
): TranscriptFoldEntry {
	const turnIds = [...new Set(items.flatMap((item) => {
		const turnId = item.message.metadata?.turn_id;
		return typeof turnId === 'string' ? [turnId] : [];
	}))];
	return { kind: 'fold', id, items, turnIds, live };
}

/**
 * What marks the run at `index` as open. A run is the stored rows it holds, so
 * its `row:` tokens follow it while it grows, closes, pages or is split by a
 * card. `after:` names the item before it, for a run opened before it holds a
 * stored row; an input keeps one identity from optimistic to delivered.
 */
export function runTokens(
	entries: TranscriptEntry[],
	index: number,
	historyComplete: boolean,
): string[] {
	const run = entries[index];
	if (run?.kind !== 'fold') return [];
	const tokens = [...(run.stub ? stubTokens(run.stub) : []), ...run.items.flatMap(({ message }) => (
		message._optimistic === true
			? []
			: ((message.mergedIds ?? [message.id]) as string[]).map((id) => `row:${id}`)
	))];
	let at = index - 1;
	while (at >= 0 && ['divider', 'pause', 'stub-pause', 'passive'].includes(entries[at].kind)) at -= 1;
	const before = entries[at];
	if (!before) return historyComplete ? [...tokens, 'after:start'] : tokens;
	const identity = before.kind === 'message'
		? before.message.metadata?.input_id ?? before.message.id
		: before.id;
	return [...tokens, `after:${identity}`];
}

export function isRunExpanded(tokens: string[], expanded: Set<string>): boolean {
	return tokens.some((token) => expanded.has(token));
}

/** Expand records every token the run has now; collapse removes them. */
export function toggleRun(tokens: string[], expanded: Set<string>): Set<string> {
	const next = new Set(expanded);
	if (isRunExpanded(tokens, expanded)) {
		for (const token of tokens) next.delete(token);
	} else {
		for (const token of tokens) next.add(token);
	}
	return next;
}

function messageEntry(
	message: TranscriptMessage,
	id: string,
	showActivity = true,
	showContent = true,
): TranscriptMessageEntry {
	return { kind: 'message', id, message, showActivity, showContent };
}

function contentOnly(message: TranscriptMessage): TranscriptMessage {
	// The open is work, so its chip goes with the activity half into the block.
	const metadata = { ...message.metadata, tool_calls: [] };
	delete metadata.browser_chip;
	return {
		...message,
		[SOURCE_MEMBERS]: sourceMembers(message),
		toolHints: [],
		reasoning: undefined,
		metadata,
	};
}

function activityOnly(message: TranscriptMessage): TranscriptMessage {
	const metadata = { ...message.metadata };
	delete metadata.for_user;
	return { ...message, [SOURCE_MEMBERS]: sourceMembers(message), content: '', metadata };
}

/** The newest chip among a block's held stubs and loaded Steps, by position: every open reopens the one browser view. */
function newestChip(stubs: readonly RunStub[], items: readonly TranscriptMessageEntry[]): BrowserChip | undefined {
	let newest: { seq: number; chip: BrowserChip } | undefined;
	const offer = (seq: number, chip: BrowserChip | null | undefined) => {
		if (chip && (!newest || seq >= newest.seq)) newest = { seq, chip };
	};
	for (const stub of stubs) offer(stub.lastSeq, stub.browserChip);
	for (const { message } of items) {
		// Items come oldest first; a row not sequenced yet (a live Step) is newer than any that is.
		const seq = typeof message.seq === 'number' && message.seq > 0 ? message.seq : Infinity;
		offer(seq, parseBrowserChip(message.metadata?.browser_chip));
	}
	return newest?.chip;
}

/**
 * Turns the already-visible transcript rows into stable Focus-mode boundaries.
 *
 * Focus has one explicit assistant-message boundary: persisted `for_user`
 * content is for the person; every other assistant row is work. Activity
 * attached to a `for_user` row remains work.
 *
 * A run ends only at a said-to-you item, so a running Turn's work joins the
 * open run after the last such item, earlier Turns' work included. `openKey`
 * keeps that run's list key unique when two presentations share one list.
 */
export function buildTranscriptPresentation(
	visibleMessages: TranscriptMessage[],
	focusMode: boolean,
	live: { turnId: string | null } | null = null,
	openKey = OPEN_RUN_KEY,
	stubs: readonly RunStub[] = [],
): TranscriptEntry[] {
	if (!focusMode) {
		return visibleMessages.map((message) => messageEntry(
			message, `message-${message.id}`,
		));
	}

	const entries: TranscriptEntry[] = [];
	const work: TranscriptMessageEntry[] = [];
	// Stubs wait for the run that reaches their position; no visible row lies between two of one run.
	const waiting = [...stubs].sort((a, b) => a.firstSeq - b.firstSeq);
	const held: RunStub[] = [];
	const holdThrough = (seq: number) => {
		while (waiting.length && waiting[0].firstSeq <= seq) {
			held.push(waiting.shift()!);
		}
	};
	const closeRun = (closingId: string | null) => {
		if (work.length === 0 && held.length === 0) return;
		const run = runEntry(work.splice(0), closingId === null ? openKey : `run:${closingId}`);
		const browserChip = newestChip(held, run.items);
		if (browserChip) run.browserChip = browserChip;
		if (held.length) {
			const stub = combineStubs(held.splice(0));
			run.stub = stub;
			run.turnIds = [...new Set([...stub.turnIds, ...run.turnIds])];
		}
		entries.push(run);
	};

	for (const message of visibleMessages) {
		if (isAgentOnlyReceipt(message)) continue;
		const at = sourceRange(message)?.min ?? message.seq;
		if (typeof at === 'number' && at > 0) holdThrough(at);
		if (isHumanMessage(message) || isSystemNotice(message)) {
			closeRun(message.id);
			entries.push(messageEntry(message, `message-${message.id}`));
			continue;
		}

		const visibleText = message.role === 'assistant'
			&& message.metadata?.for_user === true
			&& Boolean(message.content);
		if (!visibleText) {
			work.push(messageEntry(message, `work-${message.id}`));
			continue;
		}

		if (hasAssistantActivity(message)) {
			work.push(messageEntry(activityOnly(message), `work-${message.id}`, true, false));
		}
		closeRun(message.id);
		entries.push(messageEntry(contentOnly(message), `message-${message.id}`, false, true));
	}
	holdThrough(Infinity);
	closeRun(null);
	if (live) {
		const last = entries[entries.length - 1];
		if (last?.kind === 'fold') {
			last.live = true;
		} else {
			entries.push({ ...runEntry([], openKey, true), turnIds: live.turnId ? [live.turnId] : [] });
		}
	}
	return entries;
}
