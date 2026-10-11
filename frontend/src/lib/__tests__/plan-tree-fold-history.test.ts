import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { FoldedItem } from '$lib/api/plansFolded';
import type { OperatorFoldLevel } from '$lib/api/plans';
import { token } from '$lib/stores/auth';
import { node } from './fixtures/planTreeNode';

/**
 * A level's fold row says what the server counted, and opens finished work the read
 * did not list by fetching it from the plan's folded history, a page at a time.
 */

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-bearer');
});
afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
});

async function settle() {
	for (let i = 0; i < 3; i += 1) {
		await tick();
		flushSync();
		await new Promise((resolve) => setTimeout(resolve, 0));
	}
}

const item = (id: string, over: Partial<FoldedItem> = {}): FoldedItem => ({
	id, parent_id: null, kind: 'task', title: id, state: 'accepted', folded_at: '2026-09-01T00:00:00Z',
	fold_bucket: 'done', display: { label_key: 'finished', tone_key: 'complete' },
	status_reason: null, folds: { done: 0, failed: 0, closed: 0 }, has_folded_children: false, ...over,
});

const reply = (status: number, body: unknown) => ({ ok: status === 200, status, json: async () => body, headers: { get: () => null } });
const page = (items: FoldedItem[], next: string | null) => reply(200, { plan_id: 'p1', parent: 'root', items, next_cursor: next });

const root = (done: number, more: number): OperatorFoldLevel[] => [{ parent_id: null, done, failed: 0, closed: 0, more }];

function mountTree(folds: OperatorFoldLevel[]) {
	component = mount(PlanTree, {
		target: host,
		props: { nodes: [node({ id: 'open', title: 'Open task' })], folded: { planId: 'p1', levels: folds } },
	});
	flushSync();
}

const titles = () => [...host.querySelectorAll('.tree-row:not(.child-row)')].map((row) =>
	row.classList.contains('fold-row') ? `[${row.querySelector('.fold-label')?.textContent?.trim()}]` : row.querySelector('.row-title')?.textContent ?? '?');

async function press(label: string) {
	const button = [...host.querySelectorAll('button')].find((item) => item.textContent?.trim() === label);
	expect(button, `button "${label}"`).toBeTruthy();
	button!.click();
	await settle();
}

describe('PlanTree — a level\'s folded history', () => {
	it('says the server\'s count, though the read listed none of those rows', () => {
		vi.stubGlobal('fetch', vi.fn());
		mountTree(root(12, 12));
		expect(titles()).toEqual(['Open task', '[12 completed]']);
		expect(fetch).not.toHaveBeenCalled();
	});

	it('shows no fold row for a level with nothing folded', () => {
		mountTree([]);
		expect(titles()).toEqual(['Open task']);
		component && unmount(component);
		mountTree(root(0, 0));
		expect(titles()).toEqual(['Open task']);
	});

	it('pages the history five at a time, newest first, to its end, and the control then goes', async () => {
		const items = Array.from({ length: 7 }, (_, i) => item(`Old ${i}`));
		vi.stubGlobal('fetch', vi.fn(async () => page(items, null)));
		mountTree(root(7, 7));

		await press('Show 5');
		expect(titles()).toEqual(['Open task', 'Old 0', 'Old 1', 'Old 2', 'Old 3', 'Old 4', '[7 completed]']);
		await press('Show 2 more');
		expect(titles()).toHaveLength(9);
		expect([...host.querySelectorAll('button')].some((button) => button.textContent?.includes('more'))).toBe(false);
		expect(fetch).toHaveBeenCalledTimes(1);

		await press('Collapse');
		expect(titles()).toEqual(['Open task', '[7 completed]']);
	});

	it('fetches the next page by its cursor when the opened rows run out', async () => {
		const first = Array.from({ length: 3 }, (_, i) => item(`Old ${i}`));
		const second = Array.from({ length: 3 }, (_, i) => item(`Old ${i + 3}`));
		const fetcher = vi.fn()
			.mockResolvedValueOnce(page(first, 'c1'))
			.mockResolvedValueOnce(page(second, null));
		vi.stubGlobal('fetch', fetcher);
		mountTree(root(6, 6));

		await press('Show 5');
		expect(titles().filter((title) => title.startsWith('Old'))).toEqual(['Old 0', 'Old 1', 'Old 2']);
		await press('Show 3 more');
		expect(titles().filter((title) => title.startsWith('Old'))).toHaveLength(6);
		expect(String(fetcher.mock.calls[1][0])).toContain('cursor=c1');
	});

	it('keeps a cold page pending on Show while a request is on its way', async () => {
		vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
		mountTree(root(3, 3));
		await press('Show 3');
		expect(host.querySelector('button[aria-busy="true"]')?.textContent).toContain('Show 3');
		expect(host.querySelector('[role="status"]')).toBeNull();
	});

	it('re-enables Show when a cold history request rejects unexpectedly', async () => {
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('connection dropped')));
		mountTree(root(3, 3));
		await press('Show 3');
		expect(host.querySelector('button[aria-busy="true"]')).toBeNull();
		expect([...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Show 3')?.disabled).toBe(false);
	});

	it('says it could not load, and Retry reads the same page again', async () => {
		const fetcher = vi.fn()
			.mockResolvedValueOnce(reply(500, {}))
			.mockResolvedValueOnce(page([item('Old 0')], null));
		vi.stubGlobal('fetch', fetcher);
		mountTree(root(1, 1));
		await press('Show 1');
		expect(host.querySelector('[role="alert"]')?.textContent).toContain('Couldn’t load earlier finished steps.');
		expect(titles()).toEqual(['Open task', '[1 completed]']);

		await press('Retry');
		expect(host.querySelector('[role="alert"]')).toBeNull();
		expect(titles()).toEqual(['Open task', 'Old 0', '[1 completed]']);
		expect(fetcher).toHaveBeenCalledTimes(2);
	});

	it('says there is nothing to show when the history comes back empty', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => page([], null)));
		mountTree(root(3, 3));
		await press('Show 3');
		expect(host.querySelector('[role="status"]')?.textContent).toBe('No earlier finished steps to show.');
	});

	it('opens a fetched row\'s details from the row it was drawn as', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => page([item('Old 0', { title: 'Wrote the guide' })], null)));
		mountTree(root(1, 1));
		await press('Show 1');
		expect(titles()).toContain('Wrote the guide');
	});

	it('says why a fetched Won’t do row was not done, as an open one does', async () => {
		const wontDo = item('Old 0', {
			title: 'Retire the draft', state: 'closed', fold_bucket: 'closed',
			status_reason: 'No longer needed', display: { label_key: 'finished', tone_key: 'closed' },
		});
		vi.stubGlobal('fetch', vi.fn(async () => page([wontDo], null)));
		mountTree([{ parent_id: null, done: 0, failed: 0, closed: 1, more: 1 }]);
		await press('Show 1');
		expect(host.textContent).toContain('Won’t do · No longer needed');
	});
});
