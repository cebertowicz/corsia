import { get } from 'svelte/store';
import {
	appendThreadContent,
	appendThreadReasoning,
	lastAppliedSeqByThread,
	pendingDeltasByThread,
} from '../stores/threads';
import { isCurrentTurn, type TurnIdentity } from './turnState';

function sameIdentity(a: TurnIdentity | null, b: TurnIdentity): boolean {
	return a?.turnId === b.turnId && a.turnOrder === b.turnOrder;
}

function identityFromFrame(data: any): TurnIdentity | null {
	return typeof data?.turn_id === 'string' && Number.isInteger(data?.turn_order)
		? { turnId: data.turn_id, turnOrder: data.turn_order }
		: null;
}

export function beginPendingSnapshot(threadId: string): void {
	pendingDeltasByThread.update(states => {
		if (states.has(threadId)) return states;
		const next = new Map(states);
		next.set(threadId, { identity: null, deltas: [] });
		return next;
	});
}

export function isPendingSnapshot(threadId: string): boolean {
	return get(pendingDeltasByThread).has(threadId);
}

export function queuePending(threadId: string, data: any) {
	const identity = identityFromFrame(data);
	if (!identity || !isCurrentTurn(threadId, identity)) return;
	pendingDeltasByThread.update(m => {
		const n = new Map(m);
		const current = n.get(threadId);
		if (current?.identity && !sameIdentity(current.identity, identity)) return m;
		n.set(threadId, {
			identity,
			deltas: [...(current?.deltas ?? []), data],
		});
		return n;
	});
}

export function takePendingDeltas(
	threadId: string,
	identity: TurnIdentity,
): any[] {
	const pending = get(pendingDeltasByThread).get(threadId);
	pendingDeltasByThread.update(m => {
		const n = new Map(m);
		n.delete(threadId);
		return n;
	});
	if (!pending || !sameIdentity(pending.identity, identity)) return [];
	return [...pending.deltas].sort((a: any, b: any) => (a.seq ?? 0) - (b.seq ?? 0));
}

function seqAlreadyApplied(
	threadId: string,
	identity: TurnIdentity,
	seq: number | undefined,
): boolean {
	if (typeof seq !== 'number') return false;
	const last = get(lastAppliedSeqByThread).get(threadId);
	return Boolean(last && sameIdentity(last, identity) && seq <= last.seq);
}

export function markSeqApplied(
	threadId: string,
	identity: TurnIdentity,
	seq: number | undefined,
) {
	if (typeof seq !== 'number') return;
	lastAppliedSeqByThread.update(m => {
		const n = new Map(m);
		const prev = n.get(threadId);
		if (!prev || !sameIdentity(prev, identity) || seq > prev.seq) {
			n.set(threadId, { ...identity, seq });
		}
		return n;
	});
}

export function applyDelta(data: any, identity: TurnIdentity) {
	const sid = data.thread_id;
	if (!sid || !isCurrentTurn(sid, identity)) return;
	const seq = data.seq as number | undefined;
	if (seqAlreadyApplied(sid, identity, seq)) return;
	const type = data.type;
	if (type === 'text_delta' && data.content) {
		appendThreadContent(sid, data.content);
	} else if (type === 'step_reasoning' && data.content) {
		appendThreadReasoning(sid, data.content);
	}
	markSeqApplied(sid, identity, seq);
}
