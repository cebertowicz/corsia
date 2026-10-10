import { describe, it, expect } from 'vitest';
import { decodeOperatorEnvelope } from '$lib/api/plansDecode';
import type { OperatorNode } from '$lib/api/plans';
import { EMPTY_DISTRIBUTION, operatorNode, planDisplay } from './fixtures/operatorPlan';
// Built by the backend projection itself; test_operator_envelope_contract.py
// fails when the projection's shape moves away from it (#1768).
import projected from './fixtures/operator-envelope.contract.json';
import { planFoldFields } from './fixtures/planFolds';

type Json = Record<string, any>;
const projectedNodes = (body: Json): Json[] => body.plans[0].nodes;

/** The projected envelope with one node fact broken by `breakIt`. */
function broken(pick: (node: Json) => boolean, breakIt: (node: Json) => void): unknown {
	const body = structuredClone(projected) as Json;
	const node = projectedNodes(body).find(pick);
	if (!node) throw new Error('the projected fixture lacks the fact under test');
	breakIt(node);
	return body;
}

function envelope(nodes: unknown[] = [operatorNode({ id: 'a', title: 'Step' })]) {
	return {
		contract_version: 2,
		as_of: '2026-08-13T00:00:00Z',
		target: { kind: 'thread', id: 't1', label: 'Conversation' },
		revision_token: 'r1',
		attention: { awaiting_count: 0 },
		plans: [{
			plan: { id: 'plan-t1', owner_kind: 'thread', owner_id: 't1', state: 'active', revision: 1, updated_at: '2026-08-13T00:00:00Z' },
			owner: { label: 'Conversation', project_id: null, thread_id: 't1' },
			progress: { done: 0, total: 1, open: 1, liveness: 'advancing', distribution: { ...EMPTY_DISTRIBUTION } },
			display: planDisplay('working', 'active'),
			current_node_ids: ['a'],
			next_node_ids: [],
			nodes, ...planFoldFields(nodes as OperatorNode[]),
		}],
	};
}

describe('decodeOperatorEnvelope', () => {
	it('admits the envelope the pane renders, with and without plans', () => {
		expect(decodeOperatorEnvelope(envelope())).not.toBeNull();
		expect(decodeOperatorEnvelope({ ...envelope(), plans: [] })).not.toBeNull();
	});

	it('admits the envelope the backend projection actually builds', () => {
		const decoded = decodeOperatorEnvelope(projected);
		expect(decoded).not.toBeNull();
		expect(decoded!.plans[0].nodes).toHaveLength(projectedNodes(projected as Json).length);
		expect(decodeOperatorEnvelope({ ...projected, attention: {} })).toBeNull();
	});

	it('refuses a node whose details facts would throw or render undefined', () => {
		const costed = (node: Json) => !!node.commitment_cost;
		const attempted = (node: Json) => node.execution.attempts.length > 0;
		const evidenced = (node: Json) => !!node.evidence;
		const waiting = (node: Json) => node.execution.causes.length > 0;
		for (const body of [
			broken(costed, (node) => { delete node.commitment_cost.attempts; }),
			broken(costed, (node) => { node.commitment_cost.tokens.known = '12'; }),
			broken(costed, (node) => { delete node.commitment_cost.user_effort; }),
			broken(attempted, (node) => { delete node.execution.attempts[0].executor; }),
			broken(attempted, (node) => { node.execution.attempts[0].ordinal = '1'; }),
			broken(evidenced, (node) => { delete node.evidence.artifact_ids; }),
			broken(waiting, (node) => { node.execution.causes[0] = 'waiting'; }),
			broken(() => true, (node) => { delete node.requirements_revision; }),
			broken(() => true, (node) => { node.node_cost.rolled.tokens = {}; }),
		]) expect(decodeOperatorEnvelope(body)).toBeNull();
	});

	it('reads the pane-view mark when present and tolerates a backend that does not send it', () => {
		const seen = '2026-09-24T10:20:00Z';
		expect(decodeOperatorEnvelope({ ...envelope(), seen_through: seen })?.seen_through).toBe(seen);
		expect(decodeOperatorEnvelope({ ...envelope(), seen_through: null })).not.toBeNull();
		expect(decodeOperatorEnvelope(envelope())).not.toBeNull();
		expect(decodeOperatorEnvelope({ ...envelope(), seen_through: 1727173200 })).toBeNull();
	});

	it('reads the plan entry\'s folds and root count, and refuses a plan entry without them', () => {
		const bare = envelope();
		const entry = bare.plans[0];
		const level = { parent_id: null, done: 2, failed: 0, closed: 1, more: 3 };
		const withFolds = { ...bare, plans: [{ ...entry, folds: [level], root_count: 4 }] };
		expect(decodeOperatorEnvelope(withFolds)?.plans[0].folds).toEqual([level]);
		expect(decodeOperatorEnvelope(withFolds)?.plans[0].root_count).toBe(4);
		const { folds: _folds, ...noFolds } = entry;
		const { root_count: _roots, ...noRoots } = entry;
		expect(decodeOperatorEnvelope({ ...bare, plans: [noFolds] })).toBeNull();
		expect(decodeOperatorEnvelope({ ...bare, plans: [noRoots] })).toBeNull();
		expect(decodeOperatorEnvelope({ ...bare, plans: [{ ...entry, folds: [{ ...level, more: 'x' }] }] })).toBeNull();
	});

	it('refuses a body that is not the envelope', () => {
		expect(decodeOperatorEnvelope(null)).toBeNull();
		expect(decodeOperatorEnvelope('plans')).toBeNull();
		expect(decodeOperatorEnvelope({ plans: 'none' })).toBeNull();
		expect(decodeOperatorEnvelope({ ...envelope(), contract_version: 1 })).toBeNull();
		expect(decodeOperatorEnvelope({ ...envelope(), target: { id: 't1' } })).toBeNull();
	});

	it('refuses a plan entry or node missing what a template reads before it branches', () => {
		const bare = envelope();
		expect(decodeOperatorEnvelope({ ...bare, plans: [{}] })).toBeNull();
		expect(decodeOperatorEnvelope({ ...bare, plans: [{ ...bare.plans[0], progress: { done: 0 } }] })).toBeNull();
		expect(decodeOperatorEnvelope({ ...bare, plans: [{ ...bare.plans[0], nodes: [{ id: 'a', title: 'Step' }] }] })).toBeNull();
		const { display: _display, ...nodeWithoutDisplay } = operatorNode({ id: 'a' });
		expect(decodeOperatorEnvelope(envelope([nodeWithoutDisplay]))).toBeNull();
	});

	it('reads a row reason and the oversize signal only in their contract shapes', () => {
		const bare = envelope();
		const plan = (oversize: unknown) => ({ ...bare, plans: [{ ...bare.plans[0], oversize }] });
		expect(decodeOperatorEnvelope(envelope([operatorNode({ id: 'a', status_reason: 'After Build' })]))).not.toBeNull();
		expect(decodeOperatorEnvelope(envelope([operatorNode({ id: 'a', status_reason: 3 as never })]))).toBeNull();
		expect(decodeOperatorEnvelope(plan({ open: 41, budget: 40 }))).not.toBeNull();
		expect(decodeOperatorEnvelope(plan(null))).not.toBeNull();
		expect(decodeOperatorEnvelope(plan({ open: 41 }))).toBeNull();
	});

	it('reads the friction count a row subline branches on', () => {
		const withFriction = (friction: unknown) => {
			const base = operatorNode({ id: 'a' });
			return envelope([{ ...base, execution: { ...base.execution, friction_count: friction } }]);
		};
		expect(decodeOperatorEnvelope(withFriction(1))).not.toBeNull();
		expect(decodeOperatorEnvelope(withFriction(undefined))).toBeNull();
		expect(decodeOperatorEnvelope(withFriction('1'))).toBeNull();
	});

	it('refuses a nested fact that is an object but not the fact a row reads', () => {
		expect(decodeOperatorEnvelope(envelope([operatorNode({ id: 'a', branch_progress: {} as never })]))).toBeNull();
		expect(decodeOperatorEnvelope(envelope([operatorNode({ id: 'a', child: {} as never })]))).toBeNull();
		expect(decodeOperatorEnvelope(envelope([operatorNode({ id: 'a', review: { kind: 'external' } as never })]))).toBeNull();
		expect(decodeOperatorEnvelope(envelope([operatorNode({
			id: 'a', branch_progress: { done: 1, total: 2 }, review: { kind: 'external', label: 'Awaiting', actor: { kind: 'external', id: 'x', label: 'X' } },
			child: { available: true, plan_id: 'p', thread_id: 't', progress: { done: 0, total: 1, open: 1, liveness: 'advancing' } },
		})]))).not.toBeNull();
	});
});
