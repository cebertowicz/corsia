import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { nodeSubline } from '$lib/components/plans/planDisplay';
import { waitCountdown } from '$lib/components/plans/planWaits';
import { node } from './fixtures/planTreeNode';
import { withFolds } from './fixtures/planFolds';

/**
 * W11 + W12: a wait that ends soon stands out (soonest first, a countdown inside
 * the hour). Only finished work folds: a wait days away stays its own row.
 * A task scheduled until a date wears a snooze clock.
 */

const NOW = new Date(2026, 8, 25, 10, 0);
const MIN = 60_000;

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	vi.useFakeTimers();
	vi.setSystemTime(NOW);
	host = document.createElement('div');
	document.body.appendChild(host);
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.useRealTimers();
});

async function settle() {
	await tick();
	flushSync();
}

/** A waiting task ending `minutes` from now; `scheduled` says the agent chose the date. */
function wait(id: string, minutes: number | null, scheduled = false, over: Partial<OperatorNode> = {}): OperatorNode {
	const base = node({ id, title: id, ...over });
	base.display = { label_key: 'waiting', tone_key: 'waiting' };
	base.execution = {
		...base.execution,
		condition: 'waiting',
		causes: [{
			kind: 'waiting', reason_key: 'watch', reason: 'a watch', owner_kind: 'thread', owner_id: 'agent',
			ends_at: minutes === null ? null : new Date(NOW.getTime() + minutes * MIN).toISOString(),
			...(scheduled ? { scheduled_reason: 'Revisit after the review' } : {}),
		}],
	};
	return base;
}

function rendered(): string[] {
	return [...host.querySelectorAll('.tree-row')].map((row) =>
		row.classList.contains('fold-row')
			? `[${row.querySelector('.fold-label')?.textContent?.trim()}]`
			: row.querySelector('.row-title')?.textContent ?? '?',
	);
}

function row(id: string): Element {
	const found = [...host.querySelectorAll('.tree-row')].find(
		(item) => item.querySelector('.row-title')?.textContent === id,
	);
	expect(found, `row ${id}`).toBeTruthy();
	return found!;
}

describe('waits that end soonest come first within their level', () => {
	it('sorts timed waits soonest-ending first, after the level\'s other open work', async () => {
		const nodes = [
			wait('in three hours', 180),
			wait('in ten minutes', 10),
			node({ id: 'plain', title: 'Plain task' }),
			wait('in two hours', 120),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		expect(rendered()).toEqual(['Plain task', 'in ten minutes', 'in two hours', 'in three hours']);
	});

	it('puts a wait with no end time below the timed ones', async () => {
		// Behind a prerequisite the projection says blocked; the row still reads Waiting.
		const behind = wait('after a dependency', null);
		behind.execution = {
			...behind.execution,
			condition: 'blocked',
			causes: [{ kind: 'blocked', reason_key: 'prerequisite', reason: 'After Plain task', owner_kind: 'thread', owner_id: 'agent', ends_at: null }],
		};
		const nodes = [
			wait('on an event', null),
			behind,
			wait('in ten minutes', 10),
			node({ id: 'plain', title: 'Plain task' }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		expect(rendered()).toEqual(['Plain task', 'in ten minutes', 'on an event', 'after a dependency']);
	});

	it('keeps queued ready work with the open work, above every wait', async () => {
		// Ready work nobody is moving reads Waiting ("Not started"), yet nothing but
		// the agent's turn holds it, so it never sinks below a wait that may never end.
		const nodes = [
			wait('on an event', null),
			wait('in ten minutes', 10),
			node({ id: 'queued', title: 'Queued task', status_reason: 'Not started' }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		expect(rendered()).toEqual(['Queued task', 'in ten minutes', 'on an event']);
	});

	it('sorts siblings under one parent the same way', async () => {
		const nodes = [
			node({ id: 'phase', title: 'Phase', state: 'active' }),
			wait('late', 300, false, { parent_id: 'phase', depth: 1 }),
			wait('soon', 30, false, { parent_id: 'phase', depth: 1 }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes, currentNodeIds: ['soon'] }) });
		await settle();
		expect(rendered()).toEqual(['Phase', 'soon', 'late']);
	});
});

describe('a wait ending within the hour counts down', () => {
	it('says how long in minutes, and reads nothing outside the hour', () => {
		expect(waitCountdown(wait('a', 12), NOW)).toBe('ends in 12 min');
		expect(waitCountdown(wait('a', 60), NOW)).toBe('ends in 60 min');
		expect(waitCountdown(wait('a', 0.2), NOW)).toBe('ends in under a minute');
		expect(waitCountdown(wait('a', 61), NOW)).toBeNull();
		expect(waitCountdown(wait('a', -5), NOW)).toBeNull();
		expect(waitCountdown(wait('a', null), NOW)).toBeNull();
	});

	it('leads the row line, ahead of why the task waits', () => {
		expect(nodeSubline(wait('a', 12, true), NOW)).toBe('ends in 12 min · Revisit after the review');
		expect(nodeSubline(wait('a', 300, true), NOW)).toMatch(/^today at .* · Revisit after the review$/);
	});

	it('says a plain deadline once: the countdown replaces its own "Waiting until" line', () => {
		const plain = wait('a', 12);
		plain.execution.causes[0].reason = 'Waiting until 2026-09-25T17:12:00+00:00';
		plain.status_reason = plain.execution.causes[0].reason;
		expect(nodeSubline(plain, NOW)).toBe('ends in 12 min');

		const other = wait('b', 12, false, { status_reason: 'After Build the API' });
		expect(nodeSubline(other, NOW)).toBe('ends in 12 min · After Build the API');
	});

	it('shows on the rendered row and ticks down as the minutes pass', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: [wait('soon', 12, true)] }) });
		await settle();
		expect(row('soon').querySelector('.row-subline')?.textContent).toContain('ends in 12 min');
		vi.advanceTimersByTime(5 * MIN);
		await settle();
		expect(row('soon').querySelector('.row-subline')?.textContent).toContain('ends in 7 min');
	});
});

describe('only finished work folds: a wait days away stays a row', () => {
	it('keeps waits ending days out as visible rows, soonest first, outside the fold row', async () => {
		const nodes = [
			wait('a week', 7 * 24 * 60),
			wait('soon', 30),
			wait('two days', 48 * 60),
			node({ id: 'plain', title: 'Plain task' }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		expect(rendered()).toEqual(['Plain task', 'soon', 'two days', 'a week']);
		expect(host.querySelectorAll('.fold-row').length).toBe(0);
	});

	it('counts only finished work in the fold row, with open waits above it', async () => {
		const nodes = [
			wait('a week', 7 * 24 * 60),
			node({ id: 'done', title: 'Done', state: 'accepted', folded: true, last_transition_at: new Date(NOW.getTime() - 30 * 60 * MIN).toISOString() }),
		];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		expect(rendered()).toEqual(['a week', '[1 done]']);
	});
});

describe('a scheduled task wears a snooze clock; other waits keep another mark', () => {
	it('draws an SVG snooze clock for a task scheduled until a date', async () => {
		component = mount(PlanTree, { target: host, props: withFolds({ nodes: [wait('sched', 120, true)] }) });
		await settle();
		const icon = row('sched').querySelector('.state-icon');
		expect(icon?.querySelector('svg.snooze-clock')).toBeTruthy();
	});

	it('keeps the plain wait mark for an event, dependency or review wait', async () => {
		const nodes = [wait('event', null), wait('timed watch', 120)];
		component = mount(PlanTree, { target: host, props: withFolds({ nodes }) });
		await settle();
		for (const id of ['event', 'timed watch']) {
			const icon = row(id).querySelector('.state-icon');
			expect(icon?.querySelector('svg.snooze-clock')).toBeNull();
			expect(icon?.textContent?.trim()).toBe('•');
		}
	});
});
