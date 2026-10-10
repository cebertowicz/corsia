import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { OperatorNode } from '$lib/api/plans';
import { node } from './fixtures/planTreeNode';

/**
 * S1-S3: leaf tasks whose own agent scheduled a wait ending more than 24 hours
 * out, that need no one, gather into one quiet "Scheduled" row per level.
 * Everything else about a row keeps it where it was.
 */

const NOW = new Date(2026, 8, 25, 10, 0);
const MIN = 60_000;
const HOUR = 60 * MIN;

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

/** A task waiting until `hours` from now; `why` makes it one its agent scheduled, so the server rolls it up as scheduled. */
function waiting(id: string, hours: number | null, why: string | null = 'Revisit after the review', over: Partial<OperatorNode> = {}): OperatorNode {
	const base = node({ id, title: id, ...over });
	base.display = { label_key: 'waiting', tone_key: 'waiting' };
	// The server's verdict for a leaf its agent set aside; tests of what it excludes live in the backend.
	base.scheduled_until = over.scheduled_until !== undefined ? over.scheduled_until
		: hours !== null && why ? new Date(NOW.getTime() + hours * HOUR).toISOString() : null;
	base.execution = {
		...base.execution,
		condition: 'waiting',
		causes: [{
			kind: 'waiting', reason_key: 'watch', reason: 'a watch', owner_kind: 'thread', owner_id: 'agent',
			ends_at: hours === null ? null : new Date(NOW.getTime() + hours * HOUR).toISOString(),
			...(why ? { scheduled_reason: why } : {}),
		}],
	};
	return base;
}

const finished = (id: string) => node({
	id, title: id, state: 'accepted', folded: true,
	last_transition_at: new Date(NOW.getTime() - 30 * HOUR).toISOString(),
});

function titles(): string[] {
	return [...host.querySelectorAll('.tree-row')].map((row) =>
		row.classList.contains('fold-row') ? `[${row.querySelector('.fold-label')?.textContent?.trim()}]`
			: row.classList.contains('scheduled-row') ? `{${row.querySelector('.row-title')?.textContent?.trim()}}`
				: row.querySelector('.row-title')?.textContent ?? '?',
	);
}

const group = () => host.querySelector('.scheduled-row');
const text = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim();
const lines = () => [...host.querySelectorAll('.time-line')].map((el) => text(el).replace(' tasks at', ' at'));

async function press(label: string): Promise<void> {
	const button = [...host.querySelectorAll('button')].find((item) => item.textContent?.trim() === label);
	expect(button, `button "${label}"`).toBeTruthy();
	button!.click();
	await settle();
}

async function mountTree(nodes: OperatorNode[]) {
	component = mount(PlanTree, { target: host, props: { nodes } });
	await settle();
}

describe('who gathers into the Scheduled row', () => {
	it('gathers far-off scheduled tasks into one row, closed', async () => {
		await mountTree([waiting('A', 48), waiting('B', 72), waiting('C', 96)]);
		expect(titles()).toEqual(['{Scheduled}']);
		expect(group()?.querySelector('svg.snooze-clock')).toBeTruthy();
		expect(group()?.textContent).not.toContain('✓');
		expect(host.querySelector('.fold-mark')).toBeNull();
	});

	it('gathers only rows the server marks scheduled, and only past a day out', async () => {
		// The server rolls up who needs no one (user action, hold, assignment, raised ask,
		// event waits, paused, prerequisites): those rows arrive with no `scheduled_until`.
		const unmarked = ['PR wait', 'After the build', 'Needs you', 'Paused'].map((id) => waiting(id, 72, null, { scheduled_until: null }));
		const nodes = [
			waiting('Q1', 48), waiting('Q2', 72), ...unmarked,
			waiting('Within a day', 23), waiting('Overdue', -2),
		];
		await mountTree(nodes);
		const rows = titles();
		expect(rows).toContain('{Scheduled}');
		for (const id of ['PR wait', 'After the build', 'Needs you', 'Paused', 'Within a day', 'Overdue']) {
			expect(rows, id).toContain(id);
		}
		expect(rows).not.toContain('Q1');
		expect(group()?.querySelector('button')?.getAttribute('aria-label')).toBe('Show 2 scheduled tasks');
	});

	it('titles the row Scheduled with the count only on the Show control', async () => {
		await mountTree([waiting('A', 48), waiting('B', 72)]);
		expect(text(group()?.querySelector('.row-title'))).toBe('Scheduled');
		expect(text(group())).not.toMatch(/Scheduled\s*·/);
		expect(group()?.querySelector('button')?.getAttribute('aria-label')).toBe('Show 2 scheduled tasks');
	});

	describe('a parent the server marks scheduled', () => {
		const done = (id: string, parent: string) => node({ id, title: id, state: 'accepted', parent_id: parent, depth: 1 });
		/** A parent as the roll-up presents it: `scheduled_until` is the soonest end beneath it, or null. */
		const phase = (id: string, hours: number | null, over: Partial<OperatorNode> = {}) => node({
			id, title: id, kind: 'phase', state: 'active',
			scheduled_until: hours === null ? null : new Date(NOW.getTime() + hours * HOUR).toISOString(), ...over,
		});
		const mountOpen = async (nodes: OperatorNode[]) => {
			component = mount(PlanTree, { target: host, props: { nodes, expandBranchesByDefault: true } });
			await settle();
		};

		it('joins the top-level group, with its subtree beneath once the group opens', async () => {
			const kids = [done('d1', 'P'), done('d2', 'P'), waiting('Monday', 72, 'Revisit Monday', { parent_id: 'P', depth: 1 })];
			await mountOpen([phase('P', 72), ...kids]);
			expect(titles()).toEqual(['{Scheduled}']);
			expect(group()?.querySelector('button')?.getAttribute('aria-label')).toBe('Show 1 scheduled tasks');
			await press('Show 1');
			expect(titles()).toEqual(['{Scheduled}', 'P', 'Monday', 'd1', 'd2']);
		});

		it('reads as done from its children yet still groups by its own scheduled work', async () => {
			const parent = phase('Phase', 48, { display: { label_key: 'finished', tone_key: 'complete' } });
			await mountOpen([parent, done('Kid1', 'Phase'), done('Kid2', 'Phase')]);
			expect(titles()).toEqual(['{Scheduled}']);
			await press('Show 1');
			expect(titles()).toEqual(['{Scheduled}', 'Phase', 'Kid1', 'Kid2']);
		});

		it('collapses three nested levels to the top one', async () => {
			await mountOpen([
				phase('P1', 72), phase('P2', 72, { parent_id: 'P1', depth: 1 }), phase('P3', 72, { parent_id: 'P2', depth: 2 }),
				waiting('Leaf', 72, 'why', { parent_id: 'P3', depth: 3 }),
			]);
			expect(titles()).toEqual(['{Scheduled}']);
			await press('Show 1');
			expect(titles()).toEqual(['{Scheduled}', 'P1', 'P2', 'P3', 'Leaf']);
		});

		it('sorts by the soonest end beneath it', async () => {
			await mountOpen([phase('Late', 120), phase('Soonest', 30), waiting('Mid', 72)]);
			await press('Show 3');
			expect(titles()).toEqual(['{Scheduled}', 'Soonest', 'Mid', 'Late']);
		});

		it('stays an ordinary row when the server sends none, its scheduled children grouped beneath it', async () => {
			await mountOpen([
				phase('Q', null), waiting('Far2', 72, 'why', { parent_id: 'Q', depth: 1 }), node({ id: 'Busy', title: 'Busy', parent_id: 'Q', depth: 1 }),
			]);
			expect(titles()).toEqual(['Q', 'Busy', '{Scheduled}']);
		});
	});

	it('sits after the open rows and before finished work', async () => {
		await mountTree([finished('Old'), waiting('A', 48), waiting('B', 72), node({ id: 'plain', title: 'Plain' }), waiting('Imminent', 3)]);
		expect(titles()).toEqual(['Plain', 'Imminent', '{Scheduled}', '[1 completed]']);
	});

	it('gathers a single qualifying row into the same group, toggle "Show 1"', async () => {
		await mountTree([waiting('Only', 48, 'First weekly read'), node({ id: 'plain', title: 'Plain' })]);
		expect(titles()).toEqual(['Plain', '{Scheduled}']);
		expect(group()?.querySelector('button')?.getAttribute('aria-label')).toBe('Show 1 scheduled tasks');
		await press('Show 1');
		expect(titles()).toEqual(['Plain', '{Scheduled}', 'Only']);
		expect(text([...host.querySelectorAll('.tree-row')][2].querySelector('.row-subline'))).toMatch(/ · First weekly read$/);
	});
});

describe('the opened group', () => {
	it('lists members soonest first and puts a shared moment under one time line', async () => {
		const at = (hours: number) => hours;
		const nodes = [
			waiting('Late', at(120), 'reason late'),
			waiting('Nine a', at(48), 'reason a'),
			waiting('Nine b', at(48), 'reason b'),
			waiting('Early', at(30), 'reason early'),
		];
		await mountTree(nodes);
		expect(titles()).toEqual(['{Scheduled}']);
		await press('Show 4');
		expect(titles()).toEqual(['{Scheduled}', 'Early', 'Nine a', 'Nine b', 'Late']);
		expect(lines()).toHaveLength(1);
		expect(lines()[0]).toMatch(/^2 at .+/);
		// A member in a cluster says only why; one alone keeps its moment.
		const rows = [...host.querySelectorAll('.tree-row')];
		expect(text(rows[2].querySelector('.row-subline'))).toBe('reason a');
		expect(text(rows[1].querySelector('.row-subline'))).toMatch(/ · reason early$/);
	});

	it('nests each cluster as a list item holding its own list, members one level deeper', async () => {
		await mountTree([waiting('A', 48), waiting('B', 48), waiting('C', 72)]);
		await press('Show 3');
		const cluster = host.querySelector('.cluster');
		expect(cluster?.getAttribute('role')).toBe('listitem');
		expect(cluster?.querySelector('[role="list"]')?.querySelectorAll('.tree-row')).toHaveLength(2);
		const level = Number(group()!.getAttribute('aria-level'));
		for (const row of host.querySelectorAll('.tree-row:not(.scheduled-row)')) {
			expect(Number(row.getAttribute('aria-level'))).toBe(level + 1);
		}
	});

	it('names its toggle, reports its state, and keeps focus-stable "Collapse"', async () => {
		await mountTree([waiting('A', 48), waiting('B', 72)]);
		const toggle = () => group()!.querySelector('button')!;
		expect(toggle().getAttribute('aria-label')).toBe('Show 2 scheduled tasks');
		expect(toggle().getAttribute('aria-expanded')).toBe('false');
		const before = toggle();
		await press('Show 2');
		expect(toggle()).toBe(before);
		expect(toggle().textContent?.trim()).toBe('Collapse');
		expect(toggle().getAttribute('aria-expanded')).toBe('true');
		expect(toggle().getAttribute('aria-controls')?.split(' ')).toHaveLength(2);
		await press('Collapse');
		expect(titles()).toEqual(['{Scheduled}']);
	});

	it('shows a far moment with its clock and no zone', async () => {
		await mountTree([waiting('A', 24 * 20), waiting('B', 24 * 21)]);
		await press('Show 2');
		const sub = text(host.querySelector('.row-subline'));
		expect(sub).toMatch(/^[A-Z][a-z]{2} \d+ at \d+:\d{2}\s?[AP]M · /);
		expect(sub).not.toMatch(/(AM|PM)\s[A-Z]{2,5}/);
	});
});

describe('leaving the group on the minute tick', () => {
	it('moves a row out when its end comes within 24 hours, and the count drops', async () => {
		await mountTree([waiting('Soon', 24 + 2 / 60, 'r'), waiting('B', 72), waiting('C', 80)]);
		expect(titles()).toEqual(['{Scheduled}']);
		vi.advanceTimersByTime(MIN);
		await settle();
		expect(titles()).toEqual(['{Scheduled}']);
		vi.advanceTimersByTime(2 * MIN);
		await settle();
		expect(titles()).toEqual(['Soon', '{Scheduled}']);
	});

	it('keeps the last remaining row in the group when it falls to one', async () => {
		await mountTree([waiting('Soon', 24 + 1 / 60, 'r'), waiting('Other', 72)]);
		expect(titles()).toEqual(['{Scheduled}']);
		vi.advanceTimersByTime(3 * MIN);
		await settle();
		expect(titles()).toEqual(['Soon', '{Scheduled}']);
		expect(group()?.querySelector('button')?.getAttribute('aria-label')).toBe('Show 1 scheduled tasks');
	});
});

describe('the finished-work row adopts the shared toggle', () => {
	function many(count: number): OperatorNode[] {
		return Array.from({ length: count }, (_, index) => finished(`F${index}`));
	}

	it('keeps its labels and paging, names itself without "fold", and reports its state', async () => {
		await mountTree(many(12));
		const show = () => host.querySelector('.fold-row button')!;
		expect(show().textContent?.trim()).toBe('Show 5');
		expect(show().getAttribute('aria-label')).toBe('Show 5 of 12 finished steps');
		expect(show().getAttribute('aria-expanded')).toBe('false');
		expect(show().hasAttribute('aria-controls')).toBe(false);
		await press('Show 5');
		const buttons = [...host.querySelectorAll('.fold-row button')];
		expect(buttons.map((b) => b.textContent?.trim())).toEqual(['Show 5 more', 'Collapse']);
		expect(buttons[0].getAttribute('aria-label')).toBe('Show 5 of 7 finished steps');
		expect(buttons.every((b) => b.getAttribute('aria-expanded') === 'true')).toBe(true);
		expect(buttons[0].getAttribute('aria-controls')?.split(' ')).toHaveLength(5);
		await press('Show 5 more');
		await press('Show 2 more');
		expect([...host.querySelectorAll('.fold-row button')].map((b) => b.textContent?.trim())).toEqual(['Collapse']);
		expect(host.innerHTML).not.toMatch(/fold(ed)? finished/i);
	});

	it('never takes a scheduled task behind its check', async () => {
		await mountTree([...many(3), waiting('A', 48), waiting('B', 72)]);
		expect(titles()).toEqual(['{Scheduled}', '[3 completed]']);
	});
});
