import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import PlanTree from '$lib/components/plans/PlanTree.svelte';
import type { FoldedItem } from '$lib/api/plansFolded';
import { token } from '$lib/stores/auth';
import { operatorNode } from './fixtures/operatorPlan';
import { node } from './fixtures/planTreeNode';

/**
 * A row the history route fetched carries a title and a state only. Opening its details reads the
 * node (evidence, cost, time, close reason); a row the pane listed already carries them and never reads.
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
const page = (items: FoldedItem[]) => reply(200, { plan_id: 'p1', parent: 'root', items, next_cursor: null });
const full = (id: string) => operatorNode({
	id, title: id, state: 'accepted',
	evidence: { evidence: 'Merged with tests green', artifact_ids: [], requirements_revision: 1 },
});

const isNodeRead = (url: unknown) => /\/api\/plans\/p1\/nodes\/[^/]+$/.test(String(url));
const nodeReads = (fetcher: ReturnType<typeof vi.fn>) => fetcher.mock.calls.filter(([url]) => isNodeRead(url));

function mountTree(listed: ReturnType<typeof node>[] = []) {
	component = mount(PlanTree, {
		target: host,
		props: {
			nodes: [node({ id: 'open', title: 'Open task' }), ...listed],
			folded: { planId: 'p1', levels: [{ parent_id: null, done: 1, failed: 0, closed: 0, more: 1 }] },
		},
	});
	flushSync();
}

async function press(label: string) {
	const button = [...host.querySelectorAll('button')].find((candidate) => candidate.textContent?.trim() === label);
	expect(button, `button "${label}"`).toBeTruthy();
	button!.click();
	await settle();
}

async function openDetailsOf(title: string) {
	const row = [...host.querySelectorAll('.row-body')].find((body) => body.textContent?.includes(title));
	expect(row, `row "${title}"`).toBeTruthy();
	(row as HTMLElement).click();
	await settle();
}

describe('PlanTree: details of a history row', () => {
	it('reads the node when the row opens and shows its evidence', async () => {
		const fetcher = vi.fn(async (url: unknown) => (isNodeRead(url) ? reply(200, full('Old 0')) : page([item('Old 0')])));
		vi.stubGlobal('fetch', fetcher);
		mountTree();
		await press('Show 1');
		expect(nodeReads(fetcher)).toHaveLength(0);

		await openDetailsOf('Old 0');
		expect(nodeReads(fetcher)).toHaveLength(1);
		expect(String(nodeReads(fetcher)[0][0])).toBe('/api/plans/p1/nodes/Old%200');
		expect(host.querySelector('.node-details .evidence')?.textContent).toContain('Merged with tests green');
	});

	it('says it is loading while the node is on its way', async () => {
		vi.stubGlobal('fetch', vi.fn((url: unknown) => (isNodeRead(url) ? new Promise(() => {}) : Promise.resolve(page([item('Old 0')])))));
		mountTree();
		await press('Show 1');
		await openDetailsOf('Old 0');
		const loading = host.querySelector('.history-details[role="status"]');
		expect(loading?.textContent).toBe('Loading…');
		expect(host.querySelector('.node-details')).toBeNull();
	});

	it('says it could not load, and Retry reads the node again', async () => {
		let reads = 0;
		const fetcher = vi.fn(async (url: unknown) => {
			if (!isNodeRead(url)) return page([item('Old 0')]);
			reads += 1;
			return reads === 1 ? reply(500, {}) : reply(200, full('Old 0'));
		});
		vi.stubGlobal('fetch', fetcher);
		mountTree();
		await press('Show 1');
		await openDetailsOf('Old 0');
		expect(host.querySelector('.history-details[role="alert"]')?.textContent).toContain('Couldn’t load these details.');
		expect(host.querySelector('.node-details')).toBeNull();

		await press('Retry');
		expect(host.querySelector('.history-details')).toBeNull();
		expect(host.querySelector('.node-details .evidence')?.textContent).toContain('Merged with tests green');
		expect(nodeReads(fetcher)).toHaveLength(2);
	});

	it('does not read a row the pane listed', async () => {
		const fetcher = vi.fn(async () => page([]));
		vi.stubGlobal('fetch', fetcher);
		const listed = node({
			id: 'Listed', title: 'Listed done', state: 'accepted', folded: true,
			evidence: { evidence: 'Already here', artifact_ids: [], requirements_revision: 1 },
		});
		mountTree([listed]);
		await press('Show 1');
		await openDetailsOf('Listed done');
		expect(host.querySelector('.node-details .evidence')?.textContent).toContain('Already here');
		expect(nodeReads(fetcher as unknown as ReturnType<typeof vi.fn>)).toHaveLength(0);
	});
});
