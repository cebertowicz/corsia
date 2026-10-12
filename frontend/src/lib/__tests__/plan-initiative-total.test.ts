import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import { initiativeLine } from '$lib/components/plans/nodeDetailsText';
import type { OperatorCostAmount, OperatorInitiative, OperatorNode } from '$lib/api/plans';
import { decodeOperatorEnvelope } from '$lib/api/plansDecode';
import contract from './fixtures/operator-envelope.contract.json';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

const TURNS = { completed: 1, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };
const NO_TURNS = { completed: 0, error: 0, cancelled: 0, running: 0, waiting_retry: 0 };

function amount(over: Partial<OperatorCostAmount> = {}): OperatorCostAmount {
	return {
		tokens: { known: 6_400_000, unknown_rows: 0 },
		model_cost_usd: { known: 3.1, unknown_rows: 0 },
		usage_recording_failures: 0,
		execution: { seconds: 19_200, unknown_turns: 0, turns: TURNS },
		waiting: { seconds: 0 },
		...over,
	};
}

function initiative(over: Partial<OperatorInitiative> & { cost?: OperatorCostAmount } = {}): OperatorInitiative {
	return { follow_ups: 3, open: false, cost: amount(), ...over };
}

function withInitiative(value: OperatorInitiative | null): OperatorNode {
	return node({ id: 'member', initiative: value });
}

describe('initiativeLine copy', () => {
	const text = (value: OperatorInitiative | null) => {
		const line = initiativeLine(withInitiative(value));
		return line && `${line.label} ${line.segments.join(' · ')}`;
	};

	it('is absent without a lineage', () => {
		expect(text(null)).toBeNull();
	});

	it('reads tokens, working time and the follow-up count, with no dollars', () => {
		expect(text(initiative())).toBe('Initiative total: 6.4M tokens · 5h 20m working · 3 follow-ups');
	});

	it('says so far while any member is open', () => {
		expect(text(initiative({ open: true }))).toBe(
			'Initiative total so far: 6.4M tokens · 5h 20m working · 3 follow-ups',
		);
	});

	it('is singular at its smallest', () => {
		expect(text(initiative({ follow_ups: 1 }))).toBe(
			'Initiative total: 6.4M tokens · 5h 20m working · 1 follow-up',
		);
	});

	it('adds at least only from the amount own unknown-usage fields', () => {
		const partial = amount({ tokens: { known: 6_400_000, unknown_rows: 2 } });
		expect(text(initiative({ cost: partial }))).toBe(
			'Initiative total: at least 6.4M tokens · 5h 20m working · 3 follow-ups',
		);
		const failed = amount({ usage_recording_failures: 1 });
		expect(text(initiative({ cost: failed }))).toContain('at least 6.4M tokens');
		const unknownTurn = amount({ execution: { seconds: 19_200, unknown_turns: 1, turns: TURNS } });
		expect(text(initiative({ cost: unknownTurn }))).toBe(
			'Initiative total: 6.4M tokens · at least 5h 20m working · 3 follow-ups',
		);
	});

	it('names missing tokens with their unit when usage rows exist but none counts', () => {
		const unknown = amount({ tokens: { known: null, unknown_rows: 4 } });
		expect(text(initiative({ cost: unknown }))).toBe(
			'Initiative total: tokens not recorded · 5h 20m working · 3 follow-ups',
		);
	});

	it('omits the working fragment when no turn ran', () => {
		const idle = amount({ execution: { seconds: 0, unknown_turns: 0, turns: NO_TURNS } });
		expect(text(initiative({ cost: idle }))).toBe('Initiative total: 6.4M tokens · 3 follow-ups');
	});

	it('omits the tokens fragment when there are no usage rows at all', () => {
		const none = amount({ tokens: { known: null, unknown_rows: 0 } });
		expect(text(initiative({ cost: none }))).toBe('Initiative total: 5h 20m working · 3 follow-ups');
		const nothing = amount({
			tokens: { known: null, unknown_rows: 0 },
			execution: { seconds: 0, unknown_turns: 0, turns: NO_TURNS },
		});
		expect(text(initiative({ cost: nothing }))).toBe('Initiative total: 3 follow-ups');
	});

	it('reads a recorded zero as 0 tokens', () => {
		const zero = amount({
			tokens: { known: 0, unknown_rows: 0 },
			execution: { seconds: 0, unknown_turns: 0, turns: NO_TURNS },
		});
		expect(text(initiative({ cost: zero }))).toBe('Initiative total: 0 tokens · 3 follow-ups');
	});
});

describe('the Initiative total line in details', () => {
	let host: HTMLElement;
	let component: Record<string, unknown> | null = null;

	beforeEach(() => {
		host = document.createElement('div');
		document.body.appendChild(host);
	});
	afterEach(() => {
		if (component) unmount(component);
		component = null;
		host.remove();
	});

	async function open(target: OperatorNode) {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: [target] }) });
		flushSync();
		(host.querySelector('.row-body') as HTMLButtonElement).click();
		await tick();
		flushSync();
		return host.querySelector('.node-details') as HTMLElement;
	}

	const commitment = {
		commitment_id: 'member',
		attempts: { total: 3, failed: 0, correction: 0, delegated: 0 },
		tokens: { known: 25_000_000, unknown_rows: 0 },
		model_cost_usd: { known: null, unknown_rows: 0 },
		usage_recording_failures: 0,
		elapsed_seconds: { known: 60, status: 'in_progress' as const },
		user_effort: { known_plan_actions: 0, status: 'partial' as const },
	};
	const small = amount({ tokens: { known: 100, unknown_rows: 0 } });

	it('is the last line and wraps only between its segments', async () => {
		const details = await open(node({
			id: 'member', state: 'active', initiative: initiative({ open: true }),
			node_cost: { own: small, rolled: small },
		}));
		const last = details.lastElementChild as HTMLElement;
		expect(last.classList.contains('initiative')).toBe(true);
		expect(last.textContent).toBe('Initiative total so far: 6.4M tokens · 5h 20m working · 3 follow-ups');
		const segments = [...last.querySelectorAll(':scope > .segment')].map((el) => el.textContent);
		expect(segments).toEqual([
			'Initiative total so far: 6.4M tokens', '5h 20m working', '3 follow-ups',
		]);
	});

	it('replaces the older line on a lineage member', async () => {
		const details = await open(node({
			id: 'member', commitment_cost: commitment, initiative: initiative(),
			node_cost: { own: small, rolled: small },
		}));
		expect(details.querySelector('.follow-ups')).toBeNull();
		expect(details.textContent).not.toContain('With revisions and follow-ups');
		expect(details.querySelector('.initiative')).not.toBeNull();
	});

	it('leaves the older line alone everywhere else', async () => {
		const details = await open(node({
			id: 'member', commitment_cost: commitment, initiative: null,
			node_cost: { own: small, rolled: small },
		}));
		expect(details.textContent).toContain('With revisions and follow-ups: 25M tokens');
		expect(details.querySelector('.initiative')).toBeNull();
	});
});

describe('the operator envelope decoder', () => {
	it('accepts the committed contract fixture with its null initiative', () => {
		expect(decodeOperatorEnvelope(contract)).not.toBeNull();
	});

	it('rejects a malformed initiative and accepts a well-formed one', () => {
		const clone = JSON.parse(JSON.stringify(contract));
		const target = clone.plans[0].nodes[0];
		target.initiative = { follow_ups: 'x', cost: target.node_cost.rolled, open: false };
		expect(decodeOperatorEnvelope(clone)).toBeNull();
		target.initiative = { follow_ups: 2, cost: target.node_cost.rolled, open: true };
		expect(decodeOperatorEnvelope(clone)).not.toBeNull();
	});
});
