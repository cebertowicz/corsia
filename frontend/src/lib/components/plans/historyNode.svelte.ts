/**
 * One history row's details, fetched when the row is opened (`GET /api/plans/{id}/nodes/{id}`).
 * The history route sends a row's title and state only; the evidence, cost, time and close
 * reason come from this read, once per open and again on retry.
 */
import { get } from 'svelte/store';
import type { OperatorNode } from '$lib/api/plans';
import { readFoldedNode } from '$lib/api/plansFolded';
import { token } from '$lib/stores/auth';

export type HistoryNodeState =
	| { status: 'loading' }
	| { status: 'error' }
	| { status: 'ready'; node: OperatorNode };

export function createHistoryNode(input: { planId: () => string; nodeId: () => string }) {
	let state = $state.raw<HistoryNodeState>({ status: 'loading' });
	let epoch = 0;

	async function load(): Promise<void> {
		epoch += 1;
		const run = epoch;
		const planId = input.planId();
		const nodeId = input.nodeId();
		state = { status: 'loading' };
		const bearer = get(token);
		const outcome = bearer
			? await readFoldedNode(bearer, planId, nodeId)
			: ({ status: 'unauthorized' } as const);
		if (run !== epoch) return;
		state = outcome.status === 'fresh' ? { status: 'ready', node: outcome.payload } : { status: 'error' };
	}

	return {
		get state() { return state; },
		load,
		/** A read that arrives after the row closed is dropped. */
		cancel() { epoch += 1; },
	};
}
