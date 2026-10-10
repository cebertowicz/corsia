import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { activeThreadId, messages } from '$lib/stores/threads';
import { coreSettings } from '$lib/stores/settings';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import MessageList from './MessageList.svelte';

/**
 * The auto-page starts the pager's capped chain once per thread. Restarting it
 * every time a load settles would bypass the pager's cap (#2334).
 */

const THREAD_ID = 'load-older-once-thread';
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

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

describe('auto-paging a transcript that cannot scroll', () => {
	it('asks once, and not again when the load settles with history remaining', async () => {
		const onLoadOlder = vi.fn();
		const props = $state({ loadingOlderMessages: false });
		component = mount(MessageList, {
			target: host,
			props: {
				messagesEl: undefined,
				userIsNearBottom: true,
				ignoreScrollEvents: false,
				onOpenLightbox: () => {},
				hasMoreMessages: true,
				onLoadOlder,
				get loadingOlderMessages() { return props.loadingOlderMessages; },
			},
		});
		const el = host.querySelector<HTMLElement>('.messages')!;
		Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 300 });
		Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 500 });
		flushSync();
		await tick();
		await tick();
		expect(onLoadOlder).toHaveBeenCalledTimes(1);

		props.loadingOlderMessages = true;
		flushSync();
		await tick();
		props.loadingOlderMessages = false;
		flushSync();
		await tick();
		await tick();
		expect(onLoadOlder).toHaveBeenCalledTimes(1);
	});

	it('retries a Focus auto-page when an initial load settles with no visible entries', async () => {
		coreSettings.set({ features: { focus_mode: true } } as never);
		// A block counts as a shown entry, so only a row Focus omits entirely retries.
		messages.set([{ id: 'm1', thread_id: THREAD_ID, role: 'system', content: 'receipt', created_at: '2026-08-01T00:00:00Z', metadata: { type: 'activity_receipt', event_type: 'usage.recording_failed' } }] as never);
		const onLoadOlder = vi.fn();
		const props = $state({ loadingOlderMessages: true });
		component = mount(MessageList, {
			target: host,
			props: {
				messagesEl: undefined,
				userIsNearBottom: true,
				ignoreScrollEvents: false,
				onOpenLightbox: () => {},
				hasMoreMessages: true,
				onLoadOlder,
				get loadingOlderMessages() { return props.loadingOlderMessages; },
			},
		});
		const el = host.querySelector<HTMLElement>('.messages')!;
		Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 5000 });
		Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 500 });
		flushSync();
		await tick();
		await tick();
		expect(onLoadOlder).not.toHaveBeenCalled();

		props.loadingOlderMessages = false;
		flushSync();
		await tick();
		await tick();
		expect(onLoadOlder).toHaveBeenCalledOnce();
	});

	it('asks again when the thread is revisited', async () => {
		const onLoadOlder = vi.fn();
		component = mount(MessageList, {
			target: host,
			props: {
				messagesEl: undefined,
				userIsNearBottom: true,
				ignoreScrollEvents: false,
				onOpenLightbox: () => {},
				hasMoreMessages: true,
				loadingOlderMessages: false,
				onLoadOlder,
			},
		});
		const el = host.querySelector<HTMLElement>('.messages')!;
		Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 300 });
		Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => 500 });
		flushSync();
		await tick();
		await tick();
		expect(onLoadOlder).toHaveBeenCalledTimes(1);

		// A tall thread never auto-pages; coming back to the short one must.
		const other = 'load-older-once-other';
		let scrollHeight = 2000;
		Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight });
		messages.update((rows) => [
			...rows,
			{ id: 'o1', thread_id: other, role: 'assistant', content: 'x', created_at: '2026-08-01T00:00:00Z' } as never,
		]);
		activeThreadId.set(other);
		flushSync();
		await tick();
		await tick();
		expect(onLoadOlder).toHaveBeenCalledTimes(1);

		scrollHeight = 300;
		activeThreadId.set(THREAD_ID);
		flushSync();
		await tick();
		await tick();
		expect(onLoadOlder).toHaveBeenCalledTimes(2);
	});
});
