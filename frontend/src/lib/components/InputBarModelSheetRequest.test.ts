import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/ws', () => ({ on: () => () => {}, sendMessage: vi.fn(), stopTurn: vi.fn() }));
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));

import InputBar from './InputBar.svelte';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { activeModelId, models } from '$lib/stores/models';
import { activeThreadId, threads } from '$lib/stores/threads';
import { requestModelSheet } from '$lib/stores/modelSheetRequest';

const readyModel = { id: 'network/model', name: 'Network Model', size: '', backend: 'network', speed: '', status: 'ready' as const };
const mounted: Array<{ host: HTMLElement; component: ReturnType<typeof mount> }> = [];
const fetchMock = vi.fn(async (_input: RequestInfo | URL) => ({ ok: true, json: async () => ({ models: [readyModel], preview: null }) }));

beforeEach(() => {
	token.set('synthetic');
	connectionState.set('connected');
	activeThreadId.set('top-thread');
	activeModelId.set(null);
	models.set([readyModel]);
	threads.set([]);
	fetchMock.mockClear();
	vi.stubGlobal('fetch', fetchMock);
});

afterEach(async () => {
	for (const { host, component } of mounted.splice(0)) {
		await unmount(component);
		host.remove();
	}
	vi.unstubAllGlobals();
	token.set(null);
	connectionState.set('disconnected');
	activeThreadId.set(null);
	activeModelId.set(null);
	models.set([]);
	threads.set([]);
});

function render(props: { threadId?: string | null } = {}) {
	const host = document.createElement('div');
	document.body.appendChild(host);
	const component = mount(InputBar, { target: host, props: { onSend: vi.fn(), ...props } });
	mounted.push({ host, component });
	flushSync();
	return { host, component };
}

function request() {
	requestModelSheet();
	flushSync();
}

import { resizeInputBar } from './inputBarHelpers';

it.each([0, 40, 41, 150, 151])('preserves sizing at %s with state set before publishing only its container', (height) => {
	const container = document.createElement('div');
	const otherContainer = document.createElement('div');
	const area = document.createElement('div');
	container.appendChild(area);
	Object.defineProperty(area, 'offsetHeight', { value: 72 });
	const input = document.createElement('div');
	const expectedHeight = Math.min(height, 150);
	const order: string[] = [];
	Object.defineProperty(input, 'scrollHeight', { get: () => {
		expect(input.style.height).toBe('auto');
		order.push('measured');
		return height;
	} });
	const setter = (value: boolean) => {
		expect(value).toBe(expectedHeight > 40);
		expect(input.style.height).toBe(`${expectedHeight}px`);
		expect(container.style.getPropertyValue('--input-bar-height')).toBe('');
		order.push('state');
	};
	resizeInputBar(input, undefined, setter);
	expect(order).toEqual(['measured', 'state']);
	expect(container.style.getPropertyValue('--input-bar-height')).toBe('');
	order.length = 0;
	resizeInputBar(input, area, setter);
	expect(order).toEqual(['measured', 'state']);
	expect(container.style.getPropertyValue('--input-bar-height')).toBe('72px');
	expect(otherContainer.style.getPropertyValue('--input-bar-height')).toBe('');
});

const sheets = (host: HTMLElement) => host.querySelectorAll('.model-sheet');
const usageRefreshes = () => fetchMock.mock.calls.filter(([path]) => String(path) === '/api/models?refresh_usage=true&thread_id=top-thread').length;

describe('full composer model-sheet requests', () => {
	it('opens the real top sheet with an inherited nonnull binding, repeats idempotently, and reopens after close', async () => {
		const { host } = render();
		expect(host.querySelector('.composer')).not.toBeNull();
		expect(sheets(host)).toHaveLength(0);
		request();
		expect(sheets(host)).toHaveLength(1);
		expect(host.textContent).toContain('Network Model');
		expect(usageRefreshes()).toBe(1);
		request();
		request();
		expect(sheets(host)).toHaveLength(1);
		expect(usageRefreshes()).toBe(1);
		document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }));
		flushSync();
		expect(sheets(host)).toHaveLength(0);
		request();
		expect(sheets(host)).toHaveLength(1);
		expect(usageRefreshes()).toBe(2);
	});

	it('carries Local-only focus through a delayed sheet mount without a timing window', async () => {
		vi.useFakeTimers();
		threads.set([{ id: 'top-thread', user_id: 'u', profile: 'personal', title: 'Focus chat', model: null, local_only: 1, local_only_effective: true, local_only_from_project: false, local_only_inherited_from_thread_id: null, local_only_inherited_from_title: null, created_at: 'now', updated_at: 'now' }]);
		const { host } = render();
		requestModelSheet({ focusLocalOnly: true });
		vi.advanceTimersByTime(2001);
		flushSync();
		await vi.runAllTimersAsync();
		flushSync();
		expect(document.activeElement).toBe(host.querySelector('.local-only-setting'));
		vi.useRealTimers();
	});

	it.each(['leaf-thread', 'top-thread', null])('keeps an explicit %s composer out of the global request while the top opens', (threadId) => {
		const top = render();
		const leaf = render({ threadId });
		request();
		expect(sheets(top.host)).toHaveLength(1);
		expect(sheets(leaf.host)).toHaveLength(0);
		expect(usageRefreshes()).toBe(1);
	});

	it('unsubscribes on teardown and remounts without replaying an old request', async () => {
		const prior = render();
		request();
		expect(sheets(prior.host)).toHaveLength(1);
		await unmount(prior.component);
		mounted.splice(mounted.findIndex(item => item.component === prior.component), 1);
		prior.host.remove();
		fetchMock.mockClear();
		request();
		expect(usageRefreshes()).toBe(0);
		const next = render();
		expect(sheets(next.host)).toHaveLength(0);
		request();
		expect(sheets(next.host)).toHaveLength(1);
		expect(usageRefreshes()).toBe(1);
	});

	it('focuses an already-open sheet once and leaves a later plain open unfocused', async () => {
		vi.useFakeTimers();
		threads.set([{ id: 'top-thread', user_id: 'u', profile: 'personal', title: 'Focus chat', model: null, local_only: 1, local_only_effective: true, local_only_from_project: false, local_only_inherited_from_thread_id: null, local_only_inherited_from_title: null, created_at: 'now', updated_at: 'now' }]);
		const { host } = render();
		request();
		requestModelSheet({ focusLocalOnly: true });
		await vi.runAllTimersAsync();
		flushSync();
		expect(document.activeElement).toBe(host.querySelector('.local-only-setting'));
		document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }));
		flushSync();
		request();
		await vi.runAllTimersAsync();
		flushSync();
		expect(document.activeElement).not.toBe(host.querySelector('.local-only-setting'));
		vi.useRealTimers();
	});
});
