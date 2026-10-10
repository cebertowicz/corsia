import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { activeThreadId, messages } from '$lib/stores/threads';
import { coreSettings } from '$lib/stores/settings';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import MessageList from './MessageList.svelte';

/**
 * Older history must stay reachable when the loaded rows do not fill the
 * viewport: no overflow means no scroll event, so paging cannot ride on scroll
 * alone (#2334).
 */

const THREAD_ID = 'load-older-thread';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function scroller(): HTMLElement {
	const el = host.querySelector<HTMLElement>('.messages');
	expect(el).not.toBeNull();
	return el!;
}

function measure(el: HTMLElement, content: number, viewport: number): void {
	Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => content });
	Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => viewport });
}

async function renderList(opts: { content: number; hasMore?: boolean; failed?: boolean }) {
	const onLoadOlder = vi.fn();
	component = mount(MessageList, {
		target: host,
		props: {
			messagesEl: undefined,
			userIsNearBottom: true,
			ignoreScrollEvents: false,
			onOpenLightbox: () => {},
			hasMoreMessages: opts.hasMore ?? true,
			olderLoadFailed: opts.failed ?? false,
			onLoadOlder,
		},
	});
	measure(scroller(), opts.content, 500);
	flushSync();
	await tick();
	await tick();
	return onLoadOlder;
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	activeThreadId.set(THREAD_ID);
	coreSettings.set(null);
	_resetTurnStateForTests();
	messages.set([
		{ id: 'm1', thread_id: THREAD_ID, role: 'assistant', content: 'tail', created_at: '2026-08-01T00:00:00Z' },
	] as never);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	activeThreadId.set(null);
	coreSettings.set(null);
	messages.set([]);
	_resetTurnStateForTests();
});

describe('loading older messages', () => {
	it('pages on its own when the loaded rows fit in the viewport', async () => {
		const onLoadOlder = await renderList({ content: 300 });
		expect(onLoadOlder).toHaveBeenCalled();
	});

	it('does not page by itself when the transcript overflows and no one scrolled', async () => {
		const onLoadOlder = await renderList({ content: 5000 });
		expect(onLoadOlder).not.toHaveBeenCalled();
	});

	it('opens Focus history through a visible-entry continuation even when it overflows', async () => {
		coreSettings.set({ features: { focus_mode: true } } as never);
		// A block counts as shown; a row Focus omits entirely does not.
		messages.set([{ id: 'm1', thread_id: THREAD_ID, role: 'system', content: 'receipt', created_at: '2026-08-01T00:00:00Z', metadata: { type: 'activity_receipt', event_type: 'usage.recording_failed' } }] as never);
		const onLoadOlder = await renderList({ content: 5000 });

		expect(onLoadOlder).toHaveBeenCalledOnce();
		const needsMore = onLoadOlder.mock.calls[0][0] as () => boolean;
		expect(needsMore()).toBe(true);

		messages.update((rows) => [...rows, {
			id: 'reply', thread_id: THREAD_ID, role: 'assistant', content: 'Visible reply',
			created_at: '2026-09-30T00:00:00Z', metadata: { for_user: true },
		}] as never);
		flushSync();
		expect(needsMore()).toBe(false);
	});

	it('pages when the user wheels up at the very top of an overflowing transcript', async () => {
		const onLoadOlder = await renderList({ content: 5000 });
		scroller().scrollTop = 0;
		scroller().dispatchEvent(new WheelEvent('wheel', { deltaY: -50 }));
		expect(onLoadOlder).toHaveBeenCalledTimes(1);
	});

	it('does not page when the user wheels down near the top', async () => {
		const onLoadOlder = await renderList({ content: 5000 });
		scroller().scrollTop = 0;
		scroller().dispatchEvent(new WheelEvent('wheel', { deltaY: 50 }));
		expect(onLoadOlder).not.toHaveBeenCalled();
	});

	it('does not page before the first page has any rows', async () => {
		messages.set([]);
		const onLoadOlder = await renderList({ content: 0 });
		expect(onLoadOlder).not.toHaveBeenCalled();
	});

	it('does not page when there is no more history', async () => {
		const onLoadOlder = await renderList({ content: 300, hasMore: false });
		expect(onLoadOlder).not.toHaveBeenCalled();
	});

	it('offers a manual control that loads the next page', async () => {
		const onLoadOlder = await renderList({ content: 5000 });
		const button = host.querySelector<HTMLButtonElement>('button[aria-label="Load older messages"]');
		expect(button).not.toBeNull();
		button!.click();
		expect(onLoadOlder).toHaveBeenCalledTimes(1);
	});

	it('gives a Focus-mode Load older press the same visible-entry continuation', async () => {
		coreSettings.set({ features: { focus_mode: true } } as never);
		messages.update((rows) => [{
			id: 'human', thread_id: THREAD_ID, role: 'user', content: 'Show me the result',
			created_at: '2026-09-30T00:00:00Z', metadata: {},
		}, ...rows] as never);
		const onLoadOlder = await renderList({ content: 5000 });
		const button = host.querySelector<HTMLButtonElement>('button[aria-label="Load older messages"]');
		button!.click();
		const needsMore = onLoadOlder.mock.calls[0][0] as () => boolean;
		expect(needsMore()).toBe(true);

		messages.update((rows) => [...rows, {
			id: 'reply', thread_id: THREAD_ID, role: 'assistant', content: 'Visible reply',
			created_at: '2026-09-30T00:00:01Z', metadata: { for_user: true },
		}] as never);
		flushSync();
		expect(needsMore()).toBe(false);
	});

	it('does not loop on a failed load and offers Retry even with no rows loaded', async () => {
		messages.set([]);
		const onLoadOlder = await renderList({ content: 0, failed: true });
		expect(onLoadOlder).not.toHaveBeenCalled();
		const retry = host.querySelector<HTMLButtonElement>('button[aria-label="Retry loading older messages"]');
		expect(retry).not.toBeNull();
		retry!.click();
		expect(onLoadOlder).toHaveBeenCalledTimes(1);
	});
});
