import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import ThreadDoneMark from './ThreadDoneMark.svelte';
import { threadPlanStates } from '$lib/stores/threadPlanStates';
import type { ThreadPlanStateEntry } from '$lib/api/plans';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';

/**
 * Done owns slot 1 with the pause mark (AS16). The check shows only for a
 * settled plan with nothing running. Pause, an unseen failure, and unresolved
 * user action win.
 */

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const settled: ThreadPlanStateEntry = {
	state: 'idle', label_key: null, paused: false, attention_items: [], settled: true,
};

function render(entry: ThreadPlanStateEntry): void {
	threadPlanStates.set({ 'thread-1': entry });
	component = mount(ThreadDoneMark, { target: host, props: { threadId: 'thread-1' } });
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetTurnStateForTests();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	threadPlanStates.set({});
	_resetTurnStateForTests();
});

describe('ThreadDoneMark', () => {
	it('shows the check for a settled plan with nothing running', () => {
		render(settled);

		const check = host.querySelector<SVGElement>('svg.done-mark');
		expect(check?.getAttribute('aria-label')).toBe('Done');
	});

	it('renders nothing while the plan has not settled', () => {
		render({ ...settled, settled: false });

		expect(host.querySelector('svg')).toBeNull();
		expect(host.textContent).toBe('');
	});

	it('lets a pause win over a settled plan', () => {
		render({ ...settled, paused: true });

		expect(host.querySelector('svg')).toBeNull();
		expect(host.textContent).toBe('');
	});

	it('lets an unseen failure win over Done until the failure is seen', () => {
		render({
			...settled,
			state: 'failed',
			attention_items: [{ kind: 'failed', title: 'Ship the report', node_id: 'n1' }],
		});

		expect(host.querySelector('svg')).toBeNull();
		expect(host.textContent).toBe('');
	});

	it('lets unresolved user action win over Done', () => {
		render({
			...settled,
			state: 'blocked_on_you',
			attention_items: [{ kind: 'access', title: 'Allow access', node_id: null }],
		});

		expect(host.querySelector('svg')).toBeNull();
		expect(host.textContent).toBe('');
	});

	it('clears while its own turn or a descendant runs', () => {
		applyLifecycle('thread-1', { turnId: 'turn-own', turnOrder: 2 }, 'thread-1');
		render(settled);
		expect(host.querySelector('svg.done-mark')).toBeNull();

		_resetTurnStateForTests();
		applyLifecycle('subagent-of-thread-1', { turnId: 'turn-sub', turnOrder: 3 }, 'thread-1');
		render(settled);
		expect(host.querySelector('svg.done-mark')).toBeNull();
	});
});