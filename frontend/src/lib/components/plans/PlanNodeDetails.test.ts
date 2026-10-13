import { afterEach, describe, expect, it } from 'vitest';
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
