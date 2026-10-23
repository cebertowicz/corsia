import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ActivityPlanRow } from '$lib/activityHome';
import type { ThreadPlanStateEntry } from '$lib/api/plans';
import { threadPlanStates } from '$lib/stores/threadPlanStates';
import ActivityPlanRowView from './ActivityPlanRow.svelte';

const NOW = new Date(2026, 9, 2, 10, 30, 0);
const at = (minutes: number): string => new Date(NOW.getTime() + minutes * 60_000).toISOString();

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function row(threadId: string, label: string): ActivityPlanRow {
	return {
		entry: {
			plan: { revision: 1 }, owner: { thread_id: threadId, label, type: 'thread' },
			progress: { open: 1, liveness: 'waiting' }, has_nodes: false,
			display: { label_key: 'waiting', tone_key: 'waiting' }, priority_at: at(0),
		},
		thread: null, ownerType: 'agent', state: 'waiting',
	} as unknown as ActivityPlanRow;
}

function entry(extra: Partial<ThreadPlanStateEntry>): ThreadPlanStateEntry {
	return { state: 'waiting', label_key: null, paused: false, attention_items: [], ...extra };
}

function render(threadId: string): void {
	component = mount(ActivityPlanRowView, { target: host, props: { row: row(threadId, 'Release agent'), onSelectThread: () => {} } });
	flushSync();
}

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['Date'] });
	vi.setSystemTime(NOW);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	threadPlanStates.set({});
	vi.useRealTimers();
});

describe('Activity waiting rows', () => {
	it('an outside wait wears the hourglass and names its label and back time', () => {
		threadPlanStates.set({ t1: entry({ wait: { label: 'CI on PR #1193', until: at(40), more: 1 } }) });
		render('t1');

		expect(host.querySelector('.owner-copy .wait')?.textContent).toMatch(/CI on PR #1193 \+1 more · back \d{1,2}:\d{2} [AP]M/);
		expect(host.querySelector('.plan-state .waiting-mark')).not.toBeNull();
	});

	it('a set-aside row wears the snooze clock, says when it is set aside until, and has no state word', () => {
		threadPlanStates.set({ t2: entry({ state: 'idle', set_aside_until: at(24 * 60) }) });
		render('t2');

		expect(host.querySelector('.owner-copy')?.textContent).toMatch(/Set aside until /);
		expect(host.querySelector('.plan-state .snooze-clock')).not.toBeNull();
		expect(host.querySelector('.state-word')).toBeNull();
	});

	it('a plan still working or owing you keeps its own state beside set-aside work, and names its count', () => {
		for (const state of ['moving', 'needs_input'] as const) {
			threadPlanStates.set({ t6: entry({ state, set_aside_until: at(24 * 60) }) });
			const working = row('t6', 'Release agent');
			working.entry.progress = { ...working.entry.progress, open: 15, liveness: 'advancing' };
			working.entry.display = { label_key: 'working', tone_key: 'active' } as typeof working.entry.display;
			component = mount(ActivityPlanRowView, { target: host, props: { row: working, onSelectThread: () => {} } });
			flushSync();

			expect(host.textContent).not.toMatch(/Set aside/);
			expect(host.querySelector('.plan-state .snooze-clock')).toBeNull();
			expect(host.querySelector('.state-word')).not.toBeNull();
			expect(host.querySelector('.progress')?.textContent).toBe('15 tasks left');
			unmount(component);
			component = null;
		}
	});

	it('one open task reads in the singular', () => {
		threadPlanStates.set({ t7: entry({ state: 'idle', set_aside_until: at(24 * 60) }) });
		render('t7');

		expect(host.querySelector('.progress')?.textContent).toBe('1 task left');
	});

	it('a set-aside row whose time has passed reads due now', () => {
		threadPlanStates.set({ t3: entry({ state: 'idle', set_aside_until: at(-5) }) });
		render('t3');

		expect(host.querySelector('.owner-copy')?.textContent).toContain('Due now');
		expect(host.textContent).not.toMatch(/overdue|stalled|back now/i);
	});

	it('the wait link is a sibling of the row toggle; the toggle keeps its name and comes first in focus order', () => {
		threadPlanStates.set({ t4: entry({ wait: { label: 'CI on PR #1193', until: at(40), more: 0, artifact_id: 'art-1' } }) });
		render('t4');

		const link = host.querySelector<HTMLButtonElement>('button.wait-link')!;
		const toggle = host.querySelector('button.row-summary')!;
		expect(link.textContent).toBe('CI on PR #1193');
		expect(toggle.contains(link)).toBe(false);
		expect(link.parentElement!.closest('button')).toBeNull();
		expect(toggle.getAttribute('aria-label')).toBe('Open Release agent');
		expect(toggle.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
	});

	it('the set-aside subline carries the class the phone-width rule keeps visible', () => {
		threadPlanStates.set({ t5: entry({ state: 'idle', set_aside_until: at(24 * 60) }) });
		render('t5');

		expect(host.querySelector('.owner-copy > .set-aside')?.textContent).toMatch(/^Set aside until /);
	});
});
