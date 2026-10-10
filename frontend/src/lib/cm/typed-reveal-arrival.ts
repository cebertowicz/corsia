import { EditorState } from '@codemirror/state';
import { EditorView, ViewPlugin, type ViewUpdate } from '@codemirror/view';
import { CLAIM_GRACE_MS } from '$lib/stores/livePreviewFeed';
import {
	beginSelect, endSelect, userTouchedAny, userTouchedSelect, type SelectOutcome, type Selecting,
} from './typed-reveal-select';
import { adoptPreviewSelect } from './typed-reveal-claims';
import { endPreview, previewSlot } from './typed-reveal-preview';
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
	/** The tool call that posted it: a preview that call streamed is taken over instead of starting again. */
	callId?: string | null;
	/** A suggestion's committed insert; a streamed insert that it does not continue is dropped. */
	insert?: string | null;
}

type Settlements = Map<string, { threadId: string; settle: (outcome: SelectOutcome) => void }>;

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
	readonly waiting: Settlements = new Map();

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
	const taken = request.callId ? takeOver(view, settlements.waiting, { ...request, from, to }, request.callId) : null;
	if (taken) return taken;
	if (request.insert) return typeInsert(view, request, from, to);
	if (view.state.field(typedReveals).some((lane) => lane.threadId === request.threadId && lane.select)) return null;
	const select: Selecting = {
		key: request.key, from, to, ghost: null, shown: 0, length: to - from, sweepMs: request.sweepMs, holdMs: request.holdMs,
	};
	const settled = new Promise<SelectOutcome>((resolve) => settlements.waiting.set(request.key, { threadId: request.threadId, settle: resolve }));
	view.dispatch({ effects: beginSelect.of({ threadId: request.threadId, name: request.name, color: request.color, select }) });
	return settled;
}

/**
 * A suggestion nothing streamed (a non-streaming provider, a call that settled before its preview was drawn, or a
 * preview that differs from what committed) types its insert like a streamed one: the whole insert enters the
 * lane as a preview, which the committed suggestion claims, so there is one typing path. The card shows at once.
 */
function typeInsert(view: EditorView, request: ArrivalSelect, from: number, to: number): Promise<SelectOutcome> {
	const callId = request.callId ?? request.key;
	view.dispatch({ effects: previewSlot.of({
		callId, threadId: request.threadId, name: request.name, slot: 0, from, to, text: request.insert!, mode: 'suggest',
	}) });
	// A claimed preview outlives this (the suggestion owns it); one nothing claimed, because the card never showed, goes.
	setTimeout(() => { if (view.dom.isConnected) view.dispatch({ effects: endPreview.of({ callId }) }); }, CLAIM_GRACE_MS);
	return Promise.resolve('ended');
}

/**
 * The arrival of a call whose preview is in the lane. A comment adopts the highlight its preview began: the
 * hold counts from the highlight, so a slow model adds no second wait, and a highlight the user already
 * ended shows the card at once. A suggestion's insert has typed (or is typing) in place, so it needs no
 * second select. Anything else drops the preview, and the arrival runs as if there were none.
 */
function takeOver(view: EditorView, waiting: Settlements, request: ArrivalSelect, callId: string): Promise<SelectOutcome> | null {
	const lane = view.state.field(typedReveals).find((entry) => entry.threadId === request.threadId);
	const preview = lane?.previews.find((entry) => entry.callId === callId);
	if (!lane || !preview) return null;
	if (preview.mode === 'comment' && preview.selectKey && lane.select?.key === preview.selectKey) {
		const key = preview.selectKey;
		const settled = new Promise<SelectOutcome>((resolve) => waiting.set(key, { threadId: request.threadId, settle: resolve }));
		view.dispatch({ effects: adoptPreviewSelect.of({ threadId: request.threadId, key, from: request.from, to: request.to, holdMs: request.holdMs }) });
		return settled;
	}
	if (preview.mode === 'suggest' && (preview.claimId || (request.insert ?? '').startsWith(preview.text))) return Promise.resolve('ended');
	view.dispatch({ effects: endPreview.of({ callId }) });
	return preview.mode === 'comment' && preview.selectKey ? Promise.resolve('interrupted') : null;
}

/** The editor mounted inside `root`, if any: the same lookup the document canvas uses elsewhere. */
export function arrivalSelectIn(root: ParentNode | null | undefined, request: ArrivalSelect): Promise<SelectOutcome> | null {
	const content = root?.querySelector<HTMLElement>('.cm-content');
	const view = content ? EditorView.findFromDOM(content) : null;
	return view ? startArrivalSelect(view, request) : null;
}
