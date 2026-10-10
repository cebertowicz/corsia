import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/planNodeFocus', () => ({ focusPlanNode: vi.fn(async () => {}) }));

import UpdateCard from './UpdateCard.svelte';
import { focusPlanNode } from '$lib/planNodeFocus';
import type { UpdateCard as Card } from '$lib/api/threadUpdates';
import * as productEvents from '$lib/product_events';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const card: Card = {
	id: 7, thread_id: 't1', kind: 'failed', node_id: 'n1', title: 'Ship the export',
	after_seq: 3, created_at: '2026-09-29T10:00:00.000000', node_exists: true, asks_user: true, blocked_asks_user: false,
};

function render(props: { card: Card; earlier?: number }): void {
	component = mount(UpdateCard, { target: host, props });
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.mocked(focusPlanNode).mockClear();
});

describe('UpdateCard', () => {
	it('shows the snapshot title, outcome and original time, and the title opens a live item', () => {
		const spy = vi.spyOn(productEvents, 'emitProductEvent').mockImplementation(() => {});
		render({ card });

		const time = host.querySelector('time')!;
		expect(time.getAttribute('datetime')).toBe('2026-09-29T10:00:00.000000');
		expect(time.getAttribute('aria-label')).toContain('2026-09-29T10:00:00.000000');
		const titleAndOutcome = host.cloneNode(true) as HTMLElement;
		titleAndOutcome.querySelector('time')!.remove();
		expect(titleAndOutcome.textContent?.replace(/\s+/g, ' ').trim()).toBe('Ship the export · Failed');
		host.querySelector<HTMLButtonElement>('button.title')?.click();
		expect(focusPlanNode).toHaveBeenCalledWith('t1', 'n1');
		expect(spy).toHaveBeenCalledWith('update_card', 'plan_open');
		spy.mockRestore();
	});

	it('keeps a deleted item as plain text and counts earlier cards on it', () => {
		render({ card: { ...card, kind: 'done', node_exists: false, asks_user: true }, earlier: 2 });

		expect(host.querySelector('button')).toBeNull();
		expect(host.querySelector('.title')?.textContent).toBe('Ship the export');
		expect(host.querySelector('.earlier')?.textContent).toBe('2 earlier');
	});
});
