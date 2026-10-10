import { EditorState } from '@codemirror/state';
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import {
	beginSelect, endSelect, userTouchedAny, userTouchedSelect, type SelectOutcome, type Selecting,
} from './typed-reveal-select';
import { typedReveals, typedRevealOptions } from './typed-reveal-state';

export type { SelectOutcome } from './typed-reveal-select';

/** One agent comment arrival's select step: the anchor, how long it sweeps, and how long it then holds. */
export interface ArrivalSelect {
	/** The arrival's id: names the run and is the only thing its end is reported under. */
	key: string;
	threadId: string;
	name: string;
	color: string;
	from: number;
	to: number;
	sweepMs: number;
	holdMs: number;
}

/**
 * A user change or selection touching a run (or its agent's pending typed text) ends that run, and only
 * that run, as `interrupted`. It is an effect on the same transaction so the end is declared in one place.
 */
export const userEndsSelects = EditorState.transactionExtender.of((transaction) => {
	const lanes = transaction.startState.field(typedReveals, false);
	if (!lanes?.some((lane) => lane.select)) return null;
	const effects = lanes.flatMap((lane) => lane.select && (userTouchedSelect(transaction, lane.select) || userTouchedAny(transaction, lane.segments))
		? [endSelect.of({ threadId: lane.threadId, key: lane.select.key, outcome: 'interrupted' as const })] : []);
	return effects.length ? { effects } : null;
});

/** Settles each run's promise once, from what the editor did: its declared end, or `unavailable` when the run just vanished. */
const arrivalSettlements = ViewPlugin.fromClass(class {
	readonly waiting = new Map<string, { threadId: string; settle: (outcome: SelectOutcome) => void }>();

	update(update: ViewUpdate): void {
		if (!this.waiting.size) return;
		const lanes = update.state.field(typedReveals, false) ?? [];
		for (const [key, entry] of [...this.waiting]) {
			if (lanes.some((lane) => lane.threadId === entry.threadId && lane.select?.key === key)) continue;
			this.waiting.delete(key);
			const ended = update.transactions.flatMap((tr) => tr.effects).find((effect) => effect.is(endSelect) && effect.value.key === key);
			entry.settle(ended?.is(endSelect) ? ended.value.outcome : 'unavailable');
		}
	}

	destroy(): void {
		for (const entry of this.waiting.values()) entry.settle('unavailable');
		this.waiting.clear();
	}
});

export const arrivalSelects = [userEndsSelects, arrivalSettlements];

/**
 * Run a comment arrival's select step in this editor. Returns null when the editor cannot (typing not
 * allowed, nothing to select, the agent's lane already running a select); otherwise a promise that settles
 * exactly once: `ended` after sweep and hold, `interrupted` when the user acts on it, `unavailable` otherwise.
 */
export function startArrivalSelect(view: EditorView, request: ArrivalSelect): Promise<SelectOutcome> | null {
	const settlements = view.plugin(arrivalSettlements);
	const length = view.state.doc.length;
	const from = Math.max(0, request.from), to = Math.min(request.to, length);
	if (!settlements || !view.state.facet(typedRevealOptions).canType() || to <= from) return null;
	if (view.state.field(typedReveals).some((lane) => lane.threadId === request.threadId && lane.select)) return null;
	const select: Selecting = {
		key: request.key, from, to, ghost: null, shown: 0, length: to - from, sweepMs: request.sweepMs, holdMs: request.holdMs,
	};
	const settled = new Promise<SelectOutcome>((resolve) => settlements.waiting.set(request.key, { threadId: request.threadId, settle: resolve }));
	view.dispatch({ effects: beginSelect.of({ threadId: request.threadId, name: request.name, color: request.color, select }) });
	return settled;
}

/** The editor mounted inside `root`, if any: the same lookup the document canvas uses elsewhere. */
export function arrivalSelectIn(root: ParentNode | null | undefined, request: ArrivalSelect): Promise<SelectOutcome> | null {
	const content = root?.querySelector<HTMLElement>('.cm-content');
	const view = content ? EditorView.findFromDOM(content) : null;
	return view ? startArrivalSelect(view, request) : null;
}
