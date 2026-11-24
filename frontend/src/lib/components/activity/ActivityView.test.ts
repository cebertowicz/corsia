import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ActivityQueueItem } from '$lib/api/activityQueue';
import { activityFrameItems, activityQueueBody } from '$lib/__tests__/fixtures/attentionQueue';

const { mobile, mutate } = vi.hoisted(() => ({
	mobile: { store: null as null | import('svelte/store').Writable<boolean> },
	mutate: vi.fn(),
}));

vi.mock('$lib/ws/events', () => ({ on: () => () => {} }));
vi.mock('$lib/ws', () => ({ on: () => () => {} }));
vi.mock('$lib/stores/viewport', async () => {
	const { writable: w } = await import('svelte/store');
	mobile.store = w(false);
	return { isMobile: mobile.store };
});
vi.mock('$lib/api/plans', async (original) => ({
	...(await original<typeof import('$lib/api/plans')>()),
	mutateUserTask: mutate,
}));

import ActivityView from './ActivityView.svelte';
import { activityPhoneItem } from '$lib/stores/activityPhoneItem';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
/** What the next queue read returns; tests change it to model the server settling an item. */
let served: ActivityQueueItem[] = [];
let queueReads = 0;

function respond(status: number, body: unknown): Response {
	return { ok: status < 400, status, json: async () => body } as Response;
}

async function settle() {
	for (let i = 0; i < 6; i += 1) {
		await Promise.resolve();
		await tick();
	}
	flushSync();
}

function render(onSelectThread = vi.fn()) {
	component = mount(ActivityView, { target: host, props: { projectId: 'project-1', onSelectThread } });
	return onSelectThread;
}

const rows = () => [...host.querySelectorAll<HTMLButtonElement>('[data-activity-row]')];
const openRow = () => host.querySelector<HTMLButtonElement>('[data-activity-row][aria-current="true"]');
const nextButton = () => host.querySelector<HTMLButtonElement>('button.next')!;
const pane = () => host.querySelector<HTMLElement>('.pane-col')!;
const buttonNamed = (name: string) =>
	[...host.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.trim() === name);

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	served = activityFrameItems();
	queueReads = 0;
	mobile.store?.set(false);
	activityPhoneItem.set(null);
	mutate.mockReset();
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url.includes('/api/attention/queue')) {
			queueReads += 1;
			expect(url).toContain('view=activity');
			expect(url).toContain('project_id=project-1');
			return respond(200, activityQueueBody(served));
		}
		// Document lookups fail here, so a document item shows its can't-open panel instead of the editor.
		return respond(404, {});
	}));
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
});

describe('Activity list', () => {
	it('shows every kind in the ranker’s order and opens the top item with why it is there', async () => {
		render();
		await settle();
		expect(rows().map((row) => row.querySelector('.rank')?.textContent)).toEqual(['1', '2', '3', '4', '5', '6', '7']);
		expect(rows().map((row) => row.querySelector('strong')?.textContent)).toEqual(served.map((item) => item.title));
		expect(host.querySelector('.rtop h2')?.textContent).toBe('Waiting on you');
		expect(host.querySelector('.count')?.textContent).toBe('7');
		expect(host.querySelector('.by')?.textContent).toContain('Order set by CORSIA Conductor');
		expect(openRow()).toBe(rows()[0]);
		expect(openRow()?.querySelector('.why')?.textContent).toContain('0.5.0 publishes Nov 24');
		expect(rows()[1].querySelector('.why')).toBeNull();
		expect(rows()[4].querySelector('.sub')?.textContent).toContain('Packaging & Onboarding · Task');
	});

	it('Next opens the following item without acting', async () => {
		render();
		await settle();
		nextButton().click();
		await settle();
		expect(openRow()).toBe(rows()[1]);
		expect(pane().querySelector('h1')?.textContent).toBe(served[1].title);
		expect(mutate).not.toHaveBeenCalled();
	});

	it('greys Next on the last item', async () => {
		render();
		await settle();
		expect(nextButton().disabled).toBe(false);
		rows()[6].click();
		await settle();
		expect(openRow()).toBe(rows()[6]);
		expect(nextButton().disabled).toBe(true);
	});

	it('opens what followed after acting, once the server stops returning the item', async () => {
		mutate.mockImplementation(async () => {
			served = served.filter((item) => item.key !== 'task:sign-build');
			return { status: 'success' };
		});
		render();
		await settle();
		rows()[4].click();
		await settle();
		const reads = queueReads;
		buttonNamed('Done')!.click();
		await settle();
		expect(mutate).toHaveBeenCalledWith('plan-packaging', 'mark_done', 'node-sign', 4, 'mark_done:node-sign:4');
		expect(queueReads).toBeGreaterThan(reads);
		expect(rows()).toHaveLength(6);
		expect(openRow()?.querySelector('strong')?.textContent).toBe(served[4].title);
		expect(served[4].key).toBe('message:intel-mac');
		expect(host.querySelector('.count')?.textContent).toBe('6');
	});

	it('keeps a failed item open with the reason and Try again', async () => {
		mutate.mockResolvedValue({ status: 'error', message: 'Plans are read-only right now.' });
		render();
		await settle();
		rows()[4].click();
		await settle();
		buttonNamed('Done')!.click();
		await settle();
		expect(pane().querySelector('[role="alert"]')?.textContent).toBe('Plans are read-only right now.');
		expect(buttonNamed('Try again')).toBeTruthy();
		expect(openRow()).toBe(rows()[4]);
		expect(rows()).toHaveLength(7);
	});

	it('says nothing is waiting when the queue is empty', async () => {
		served = [];
		render();
		await settle();
		expect(host.textContent).toContain('Nothing is waiting on you.');
		expect(rows()).toHaveLength(0);
		expect(pane().textContent).toContain('Nothing to open.');
	});

	it('says the read failed and offers Try again', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => respond(500, {})));
		render();
		await settle();
		expect(host.querySelector('[role="alert"]')?.textContent).toContain('Activity couldn’t load.');
		expect(buttonNamed('Try again')).toBeTruthy();
	});

	it('no longer shows the old sections', async () => {
		render();
		await settle();
		const text = host.textContent ?? '';
		for (const gone of ['Continue', 'Needs you', 'In progress', 'Complete (', 'Measures']) expect(text).not.toContain(gone);
		expect(host.querySelector('.row-summary, .section-toggle, .activity-measures, .needs-you, #activity-work-title')).toBeNull();
	});
});

describe('Activity on a phone', () => {
	it('shows the list, then the item alone with position and Next in the header', async () => {
		mobile.store!.set(true);
		render();
		await settle();
		const view = host.querySelector('.activity')!;
		expect(view.classList.contains('phone')).toBe(true);
		expect(view.classList.contains('phone-item')).toBe(false);
		expect(get(activityPhoneItem)).toBeNull();

		rows()[1].click();
		await settle();
		expect(view.classList.contains('phone-item')).toBe(true);
		const header = get(activityPhoneItem)!;
		expect(header).toMatchObject({ position: 2, count: 7, nextDisabled: false });

		header.onNext();
		await settle();
		expect(get(activityPhoneItem)).toMatchObject({ position: 3, count: 7 });

		get(activityPhoneItem)!.onBack();
		await settle();
		expect(view.classList.contains('phone-item')).toBe(false);
		expect(get(activityPhoneItem)).toBeNull();
		expect(openRow()).toBe(rows()[2]);
	});
});
