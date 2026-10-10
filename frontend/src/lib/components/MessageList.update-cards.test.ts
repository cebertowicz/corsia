import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { activeThreadId, activeToolsByThread, messages } from '$lib/stores/threads';
import { threadUpdateCards, visitDividers } from '$lib/stores/threadUpdateCards';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import MessageList from './MessageList.svelte';

/** Plan update cards sit in the transcript at their position, under this visit's frozen divider. */

const THREAD = 'cards-thread';
const OTHER = 'other-thread';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let fetchMock: ReturnType<typeof vi.fn>;

function row(seq: number, threadId = THREAD) {
	return {
		id: `${threadId}-m${seq}`, thread_id: threadId, role: 'user' as const, content: `message ${seq}`,
		created_at: '2026-09-29T10:00:00', seq,
	};
}

function page(url: string) {
	const withDivider = url.includes('divider=true');
	return {
		cards: url.includes(THREAD) ? [{
			id: 1, thread_id: THREAD, kind: 'done', node_id: null, title: 'Ship the export',
			after_seq: 1, created_at: '2026-09-29T08:00:00.000000', node_exists: false, asks_user: true,
		}] : [],
		has_more: false,
		divider: withDivider ? { since: '2026-09-29T09:00:00.000000', first_seq: 2 } : null,
	};
}

async function settle(): Promise<void> {
	for (let i = 0; i < 5; i++) await tick();
	flushSync();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetTurnStateForTests();
	activeToolsByThread.set(new Map());
	threadUpdateCards.set({});
	visitDividers.set({});
	fetchMock = vi.fn(async (url: string) => ({ ok: true, json: async () => page(String(url)) }));
	vi.stubGlobal('fetch', fetchMock);
	token.set('bearer');
	messages.set([row(1), row(2), row(1, OTHER)]);
	activeThreadId.set(THREAD);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	messages.set([]);
	token.set(null);
	vi.unstubAllGlobals();
});

function order(): string[] {
	return [...host.querySelectorAll('[data-message-id], .update-card, [data-visit-divider]')].map((el) => (
		el.hasAttribute('data-visit-divider') ? 'divider'
			: el.classList.contains('update-card') ? 'card' : el.getAttribute('data-message-id')!
	));
}

describe('MessageList update cards and the visit divider', () => {
	it('places the card after its message and the divider before the first new one', async () => {
		component = mount(MessageList, {
			target: host,
			props: { messagesEl: undefined, userIsNearBottom: true, ignoreScrollEvents: false, onOpenLightbox: () => {} },
		});
		await settle();

		expect(order()).toEqual([`${THREAD}-m1`, 'card', 'divider', `${THREAD}-m2`]);
		expect(String(fetchMock.mock.calls[0][0])).toContain('divider=true');
	});

	it('ends the visit on leaving, so the next open reads a fresh divider', async () => {
		component = mount(MessageList, {
			target: host,
			props: { messagesEl: undefined, userIsNearBottom: true, ignoreScrollEvents: false, onOpenLightbox: () => {} },
		});
		await settle();

		activeThreadId.set(OTHER);
		await settle();
		expect(THREAD in get(visitDividers)).toBe(false);
		expect(order()).toEqual([`${OTHER}-m1`]);

		activeThreadId.set(THREAD);
		await settle();
		const reads = fetchMock.mock.calls.map(([url]) => String(url)).filter((url) => url.includes(THREAD));
		expect(reads.filter((url) => url.includes('divider=true'))).toHaveLength(2);
		expect(order()).toContain('divider');
	});
});
