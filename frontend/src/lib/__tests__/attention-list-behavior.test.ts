/**
 * L2 component-behavior tests for Requires User reconnect recovery.
 */

import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import AttentionList from '$lib/components/AttentionList.svelte';
import { fetchRaised, type AttentionItem } from '$lib/api/attention';
import { token } from '$lib/stores/auth';
import { emit } from '$lib/ws/events';

vi.mock('$lib/api/attention', () => ({
	clearAttention: vi.fn(),
	disposeAttention: vi.fn(),
	fetchRaised: vi.fn(),
	fetchReview: vi.fn(),
}));

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeAll(() => {
	if (!Element.prototype.animate) {
		Element.prototype.animate = vi.fn(() => ({
			finished: Promise.resolve(),
			cancel: vi.fn(),
			commitStyles: vi.fn(),
			onfinish: null,
			playState: 'finished',
		})) as unknown as Element['animate'];
	}
});

const HIGH: AttentionItem = {
	external_id: 'high',
	integration: 'github',
	type: 'pull-request',
	attention_state: 'raised',
	urgency: 'high',
	requested_action: 'High action',
	owner_thread: 'IMPL · Coordination MVP',
	levels: [],
};

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('tok');
	vi.mocked(fetchRaised).mockReset();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	vi.restoreAllMocks();
});

async function settle() {
	await tick();
	await Promise.resolve();
	flushSync();
}

describe('AttentionList reconnect refresh', () => {
	it('refetches canonical attention state after authenticated reconnect', async () => {
		vi.mocked(fetchRaised)
			.mockResolvedValueOnce({ items: [HIGH] })
			.mockResolvedValueOnce({ items: [] });
		component = mount(AttentionList, {
			target: host,
			props: { mode: 'user', threadId: 'thread-1' },
		});
		await settle();
		expect(host.textContent).toContain('High action');

		emit('connected', {});

		await vi.waitFor(() => {
			expect(fetchRaised).toHaveBeenCalledTimes(2);
			expect(host.textContent).not.toContain('High action');
		});
	});

	it('ignores an older response that finishes after reconnect refresh', async () => {
		let finishInitial!: (value: { items: AttentionItem[] }) => void;
		const initial = new Promise<{ items: AttentionItem[] }>((resolve) => {
			finishInitial = resolve;
		});
		vi.mocked(fetchRaised)
			.mockReturnValueOnce(initial)
			.mockResolvedValueOnce({ items: [] });
		component = mount(AttentionList, {
			target: host,
			props: { mode: 'user', threadId: 'thread-1' },
		});
		await vi.waitFor(() => expect(fetchRaised).toHaveBeenCalledTimes(1));

		emit('connected', {});
		await vi.waitFor(() => expect(fetchRaised).toHaveBeenCalledTimes(2));

		finishInitial({ items: [HIGH] });
		await settle();
		expect(host.textContent).not.toContain('High action');
	});
});
