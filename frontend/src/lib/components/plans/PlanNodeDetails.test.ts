import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import type { OperatorNode } from '$lib/api/plans';
import { operatorNode } from '$lib/__tests__/fixtures/operatorPlan';
import PlanNodeDetails from './PlanNodeDetails.svelte';

let host: HTMLElement;
let component: { $destroy: () => void } | null = null;

function selectedChild(statusReason: string | null, primaryReason: string | null = null): OperatorNode {
	const base = operatorNode({
		state: 'active',
		title: 'Selected child task',
		display: { label_key: 'waiting', tone_key: 'waiting' },
		status_reason: statusReason,
	});
	return {
		...base,
		execution: {
			...base.execution,
			condition: 'running',
			primary_reason: primaryReason,
			selection: {
				attempt_id: 'attempt', attempt_ordinal: 1, executor_thread_id: 'agent',
				requirements_revision: 1, turn_active: false, in_flight_dispatch_count: 0,
			},
		},
	};
}

function render(node: OperatorNode): HTMLElement {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = createClassComponent({
		target: host, component: PlanNodeDetails, props: { node, id: 'selected-details' },
	});
	flushSync();
	return host.querySelector('.status')!;
}

afterEach(() => {
	component?.$destroy();
	component = null;
	host?.remove();
	vi.useRealTimers();
});

const held = 'Stalled: its input is held for its account';

describe('selected child account details', () => {
	it.each([
		['signed out', `${held} Its account is not ready: it is signed out. Sign in to the permitted account through the account controls.`],
		['unread', `${held} Its account readiness could not be read. Inspect the held work before taking action.`],
		['claim pending', `${held} Its account is ready; the input is waiting for its claim to resume.`],
	])('renders the full %s advisory reason when the execution reason is absent', (_state, reason) => {
		const status = render(selectedChild(reason));
		expect(status.querySelector('.word')?.textContent).toBe('Waiting');
		expect(status.querySelector('.reason')?.textContent).toBe(` · ${reason}`);
	});

	it('keeps the primary execution reason ahead of the advisory account reason', () => {
		const status = render(selectedChild(`${held} Its account readiness could not be read.`, 'Waiting for the delegated child to return'));
		expect(status.querySelector('.reason')?.textContent).toBe(' · Waiting for the delegated child to return');
		expect(status.textContent).not.toContain(held);
	});

	it('keeps a reasonless status free of an empty separator', () => {
		const status = render(selectedChild(null));
		expect(status.querySelector('.reason')).toBeNull();
		expect(status.textContent).not.toContain(' · ');
	});

	it('updates displayed open durations from the shared clock without another read', () => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-01-01T00:00:10Z'));
		const base = operatorNode({});
		const cost = { ...base.node_cost.rolled,
			execution: { ...base.node_cost.rolled.execution, settled_seconds: 2,
				open_starts: ['2026-01-01T00:00:00Z'],
				turns: { ...base.node_cost.rolled.execution.turns, running: 1 } },
			waiting: { settled_seconds: 1, open_starts: ['2026-01-01T00:00:00Z'] },
		};
		render({ ...base, node_cost: { own: cost, rolled: cost } });
		expect(host.textContent).toContain('12s');
		expect(host.textContent).toContain('11s');
		vi.advanceTimersByTime(15_000);
		flushSync();
		expect(host.textContent).toContain('27s');
		expect(host.textContent).toContain('26s');
	});

	it('renders a person step’s instructions with code copy and the steps it releases', () => {
		render(operatorNode({
			assignee: 'user',
			description: 'Run this command.\n\n```sh\ncorsia verify\n```',
			waiting_on: ['Publish the release', 'Notify the team'],
		}));

		expect(host.querySelector('.person-instructions')?.textContent).toContain('Run this command.');
		expect(host.querySelector('.person-instructions code')?.textContent).toBe('corsia verify');
		expect(host.querySelector('.person-instructions .copy-btn')?.getAttribute('aria-label')).toBe('Copy code');
		expect(host.querySelector('.waiting-on')?.textContent).toBe(
			'Waiting on this: Publish the release, Notify the team',
		);
	});
});

describe('a Later member’s reason', () => {
	const LONG = 'Waiting on the design review of the mapping rules, the security sign-off for the new bucket, '
		+ 'and the pilot customer’s confirmation that the legacy encoding can be dropped.';

	it('reads whole below the status for a member with no date', () => {
		render(operatorNode({ later: true, later_reason: LONG }));
		expect(host.querySelector('.later-reason')?.textContent).toBe(`Later: ${LONG}`);
	});

	it('reads whole for a dated member as well, whose row subline carries only its date', () => {
		const due = new Date(Date.now() + 48 * 3_600_000).toISOString();
		render(operatorNode({ scheduled_until: due, later_reason: LONG }));
		expect(host.querySelector('.later-reason')?.textContent).toBe(`Later: ${LONG}`);
	});

	it('is not raised for work that is not Later', () => {
		render(operatorNode({ later: false, later_reason: LONG }));
		expect(host.querySelector('.later-reason')).toBeNull();
	});

	it('is left out when the agent gave no reason', () => {
		render(operatorNode({ later: true, later_reason: null }));
		expect(host.querySelector('.later-reason')).toBeNull();
	});
});
