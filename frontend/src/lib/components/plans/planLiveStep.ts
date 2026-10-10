/**
 * What a plan item's running work is doing now (PRD-ATTENTION-SIGNAL AS14).
 *
 * The plan projection names who works an item: its delegated worker
 * (`child.thread_id`) or, while the item is selected, its own executor. Whether
 * that thread runs and when its Turn started come from the live Turn state, and
 * its current step from the tool store the transcript reads; the subscribe
 * snapshot fills both on reload. Row presentation is derived on every render.
 * One transient phrase per thread and durable Turn start survives row mounts
 * solely so visibility can move between parent and child rows without losing
 * what that same Turn already showed; none of it is persisted.
 */
import { readable } from 'svelte/store';
import type { OperatorNode } from '$lib/api/plans';
import type { ActiveTool } from '$lib/stores/threads';
import type { ThreadTurnState } from '$lib/ws/turnState';

export interface LiveStep {
	threadId: string;
	phrase: string;
	/** True between tools: the phrase is the last tool's, shown dimmed, not a new state. */
	between: boolean;
	/** True when no tool phrase is known: the Turn has shown none, or the tool store is empty. */
	unknown: boolean;
	/** The running Turn's durable start, or null when no frame has named it. */
	startedAt: number | null;
}

/**
 * The phrase for a running Turn with no tool phrase known: before its first
 * tool, or after a reload. It claims only that the work runs.
 */
export const UNKNOWN_STEP = 'Working';

/** How long a phrase stays up before a newer one replaces it, so fast tools don't strobe. */
export const PHRASE_HOLD_MS = 1200;
/** The opacity dip between two phrases. */
export const FADE_MS = 160;
/** How long a row keeps its step after its Turn ends, bridging the gap to a successor Turn. */
export const LINGER_MS = 2000;

/** Milliseconds left before the phrase shown at `shownAt` may be replaced. */
export function holdRemaining(shownAt: number, now: number): number {
	return Math.max(0, shownAt + PHRASE_HOLD_MS - now);
}

/** The threads whose running Turn would be this item's work: its worker first. */
export function liveStepThreads(node: OperatorNode): string[] {
	const execution = node.execution;
	if (execution.condition === 'terminal') return [];
	const threads: string[] = [];
	if (node.child?.thread_id) threads.push(node.child.thread_id);
	if (execution.condition === 'running' && execution.selection) {
		threads.push(execution.selection.executor_thread_id);
	}
	return threads;
}

/**
 * Whether the server shows this step's own work moving: it runs, or work moves
 * at any depth under its delegated worker, which no thread known to the pane
 * may be doing (`operator_node_state.display`). A step awaiting its agent's
 * review also reads Working, and nothing moves.
 */
function stepMoves(node: OperatorNode): boolean {
	const condition = node.execution.condition;
	return node.display.label_key === 'working' && condition !== 'review' && condition !== 'terminal';
}

/** A row's ancestors on this plan. A corrupt parent cycle ends the walk, as it does in `ancestorChain`. */
function ancestorsOf(nodeId: string, parentOf: ReadonlyMap<string, string | null>): string[] {
	const seen = new Set([nodeId]);
	const ancestors: string[] = [];
	for (let up = parentOf.get(nodeId); up && parentOf.has(up) && !seen.has(up); up = parentOf.get(up)) {
		seen.add(up);
		ancestors.push(up);
	}
	return ancestors;
}

/** The one roll-up for a row's spin and live line, read the same way by a branch and a delegated step. */
export interface LiveRollup {
	/** Every thread whose running Turn is work at or under each row: its own first, then its descendants' in plan order. */
	threads: Map<string, string[]>;
	/** Rows with a step at or beneath them that the server shows moving (`stepMoves`). */
	moving: Set<string>;
}

export function rolledLive(nodes: readonly OperatorNode[]): LiveRollup {
	const threads = new Map(nodes.map((node) => [node.id, liveStepThreads(node)]));
	const parentOf = new Map(nodes.map((node) => [node.id, node.parent_id]));
	const branches = new Set(nodes.map((node) => node.parent_id));
	const moving = new Set<string>();
	for (const node of nodes) {
		const own = liveStepThreads(node);
		// A branch's label is its steps' rolled up, so only a step says whether work moves.
		const moves = !branches.has(node.id) && stepMoves(node);
		if (own.length === 0 && !moves) continue;
		for (const up of ancestorsOf(node.id, parentOf)) {
			const carried = threads.get(up)!;
			for (const threadId of own) if (!carried.includes(threadId)) carried.push(threadId);
			if (moves) moving.add(up);
		}
		if (moves) moving.add(node.id);
	}
	return { threads, moving };
}

/**
 * The threads the rows rendered directly under each row already show, by
 * parent. The line sits on the lowest rendered row carrying it, so a row drops
 * a thread these carry; a row that is not rendered carries none.
 */
export function carriedByParent(
	visibleNodes: readonly OperatorNode[],
	threads: ReadonlyMap<string, readonly string[]>,
): Map<string, string[]> {
	const below = new Map<string, string[]>();
	for (const row of visibleNodes) {
		if (!row.parent_id) continue;
		const carried = below.get(row.parent_id) ?? [];
		carried.push(...(threads.get(row.id) ?? []));
		below.set(row.parent_id, carried);
	}
	return below;
}

/** What a row remembers between renders to decide its live line. */
export interface LiveLine {
	/** The last step the row showed live, kept while it lingers. */
	last: LiveStep | null;
	/** Absolute time the linger ends: set once when the Turn ends, never extended. */
	deadline: number | null;
	/** The caller's segment in which `last` was seen; unset when the caller names none. */
	segment?: string;
}

export const NO_LIVE_LINE: LiveLine = { last: null, deadline: null };

/**
 * The last concrete phrase a running Turn has shown, one entry per thread.
 *
 * `LiveLine.last` is memory inside one mounted row, so the row a branch
 * expansion mounts mid-Turn has none: once a `step_text` frame empties the tool
 * store it would read UNKNOWN_STEP for work the Turn already showed. Kept here
 * so the same Turn's phrase reaches whichever row owns visibility, guarded by
 * the Turn's durable start: a successor Turn (a new start) never inherits it,
 * and a Turn no frame has named (null start) never reaches it at all.
 */
const lastPhraseByThread = new Map<string, { startedAt: number; step: LiveStep }>();

/** Only a concrete phrase is worth keeping; UNKNOWN_STEP would carry nothing. */
function rememberPhrase(live: LiveStep): void {
	if (live.startedAt !== null && !live.unknown) {
		lastPhraseByThread.set(live.threadId, { startedAt: live.startedAt, step: live });
	}
}

/** The phrase this Turn last showed, for a row that mounts after the store emptied. */
function recallPhrase(threadId: string, startedAt: number): LiveStep | null {
	const remembered = lastPhraseByThread.get(threadId);
	return remembered && remembered.startedAt === startedAt ? remembered.step : null;
}

/** Test seam: this memory outlives a mount, so a fresh test starts from empty. */
export function _resetLiveStepForTests(): void {
	lastPhraseByThread.clear();
}

/**
 * The one owner of a row's live-line display. Inputs are the step now, whether
 * the node is terminal and the time; outputs are the next row memory and the
 * step to show (dimmed while lingering) or null. Seeing a concrete phrase also
 * records it in the transient per-thread/per-Turn handoff memory above. A
 * terminal node always clears; a successor Turn inside the linger window
 * replaces it without a gap; a plan refresh re-runs this with the same
 * deadline, so it cannot extend the linger.
 *
 * A caller whose line restarts inside one Turn (Focus, after a shown update)
 * names its `segment`. A phrase then carries only from this row's own memory of
 * the same segment, never from the per-thread memory, which would bring back a
 * phrase from before the update. With no segment nothing here changes.
 */
export function advanceLiveLine(
	prev: LiveLine,
	live: LiveStep | null,
	terminal: boolean,
	now: number,
	segment?: string,
): { line: LiveLine; show: LiveStep | null } {
	const inSegment = segment === undefined ? {} : { segment };
	if (terminal) {
		if (prev.last && prev.last.startedAt !== null) lastPhraseByThread.delete(prev.last.threadId);
		return { line: NO_LIVE_LINE, show: null };
	}
	if (live) {
		// Same Turn, no phrase known now: keep the last one dimmed, never flip to "Working".
		// This row's own last phrase, or — for a row that mounted late — the Turn's shared one.
		// A Turn no frame has named (null start) cannot be told from its successor, so it never carries.
		const last = prev.last;
		const sameTurn = live.startedAt !== null && last?.threadId === live.threadId && last.startedAt === live.startedAt;
		if (live.unknown && live.startedAt !== null) {
			const own = sameTurn && last && !last.unknown && prev.segment === segment ? last : null;
			const carried = own ?? (segment === undefined ? recallPhrase(live.threadId, live.startedAt) : null);
			if (carried) {
				return {
					line: { last: carried, deadline: null, ...inSegment },
					show: { ...carried, threadId: live.threadId, startedAt: live.startedAt, between: true },
				};
			}
		}
		rememberPhrase(live);
		return { line: { last: live, deadline: null, ...inSegment }, show: live };
	}
	if (!prev.last) return { line: NO_LIVE_LINE, show: null };
	if (prev.last.startedAt !== null) lastPhraseByThread.delete(prev.last.threadId);
	const deadline = prev.deadline ?? now + LINGER_MS;
	if (now >= deadline) return { line: NO_LIVE_LINE, show: null };
	return { line: { last: prev.last, deadline }, show: { ...prev.last, between: true } };
}

function currentPhrase(tools: readonly ActiveTool[]): Pick<LiveStep, 'phrase' | 'between' | 'unknown'> {
	for (let index = tools.length - 1; index >= 0; index -= 1) {
		const tool = tools[index];
		if (tool.status === 'running') return { phrase: tool.phrase || tool.name, between: false, unknown: false };
	}
	// The last finished tool stays up, dimmed. The store also empties mid-Turn (a step_text
	// frame drops finalized tools), so an empty list is unknown here; advanceLiveLine keeps
	// the row's last phrase over it.
	const last = tools[tools.length - 1];
	return last
		? { phrase: last.phrase || last.name, between: true, unknown: false }
		: { phrase: UNKNOWN_STEP, between: false, unknown: true };
}

/** What one thread's running Turn is doing now, or null when no Turn runs. */
export function threadLiveStep(
	threadId: string,
	turns: ReadonlyMap<string, ThreadTurnState>,
	toolsByThread: ReadonlyMap<string, ActiveTool[]>,
): LiveStep | null {
	const turn = turns.get(threadId);
	if (turn?.phase !== 'active') return null;
	return {
		threadId,
		...currentPhrase(toolsByThread.get(threadId) ?? []),
		startedAt: turn.startedAt,
	};
}

/** The first of a row's rolled threads (`rolledLive`) whose Turn runs now. */
export function planLiveStep(
	threads: readonly string[],
	turns: ReadonlyMap<string, ThreadTurnState>,
	toolsByThread: ReadonlyMap<string, ActiveTool[]>,
): LiveStep | null {
	for (const threadId of threads) {
		const step = threadLiveStep(threadId, turns, toolsByThread);
		if (step) return step;
	}
	return null;
}

/** Whole minutes: the subline counts work, it is not a stopwatch. */
export function elapsedLabel(ms: number): string {
	const minutes = Math.floor(Math.max(0, ms) / 60_000);
	if (minutes < 1) return 'under 1 min';
	if (minutes < 60) return `${minutes} min`;
	const hours = Math.floor(minutes / 60);
	const rest = minutes % 60;
	return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

export function liveStepLabel(step: LiveStep, now: number): string {
	if (step.startedAt === null) return step.phrase;
	const elapsed = elapsedLabel(now - step.startedAt);
	return step.phrase === UNKNOWN_STEP ? `Working ${elapsed}` : `Working ${elapsed} · ${step.phrase}`;
}

const TICK_MS = 15_000;

/** One shared clock for every live row, running only while a row shows. */
export const liveStepClock = readable(Date.now(), (set) => {
	set(Date.now());
	const timer = setInterval(() => set(Date.now()), TICK_MS);
	return () => clearInterval(timer);
});
