import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import InputBarModelSheet from './InputBarModelSheet.svelte';
import { token } from '$lib/stores/auth';
import { composerSelectionFor } from '$lib/stores/composerSelection';
import {
	_resetThreadModelRequestsForTests, _resetThreadModelTruthForTests,
	acceptThreadModelTruth, activeModelId, models, threadModelRequests,
} from '$lib/stores/models';
import { threadCatalogs } from '$lib/stores/modelsState';
import { subagentMeta, subagentThreadRows } from '$lib/stores/subagents';
import { activeThreadId, threads } from '$lib/stores/threads';
import { toasts } from '$lib/stores/toast';
import { handleMessage } from '$lib/ws/handlers';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
vi.mock('$lib/stores/localPickerStatus', async () => {
	const { readable } = await import('svelte/store');
	return { localStatus: readable(null), canAddLocalModel: readable(false), canSetUpLocalModel: readable(false), neverLocalHost: readable(false), retryLocalSetup: vi.fn(), watchLocalStatus: () => () => {} };
});

let host: HTMLElement;
let component: ReturnType<typeof mount> | undefined;
let resolvePatch: (response: Response) => void;
let rejectPatch: (error: Error) => void;
const originalFetch = globalThis.fetch;
const previous = 'omlx@fixture/current';
const target = 'omlx@fixture/next';

function row(id: string) {
	return { id, name: id, size: '', backend: 'network', provider: 'omlx', speed: '', status: 'ready', litellm_model: id } as const;
}

function response(body: object, status = 200) {
	return new Response(JSON.stringify(body), { status });
}

function mountSheet(id = 'chat') {
	component = mount(InputBarModelSheet, { target: host, props: { threadId: id } });
	flushSync();
}

function clickNext() {
	const button = [...host.querySelectorAll<HTMLButtonElement>('button.model-list-item')].find((b) => b.textContent?.includes(target))!;
	button.click();
	flushSync();
	return button;
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	_resetThreadModelRequestsForTests();
	_resetThreadModelTruthForTests();
	token.set('synthetic-test-token');
	activeThreadId.set('chat');
	activeModelId.set(previous);
	threads.set([{ id: 'chat', model: previous, updated_at: '2026-11-06T19:00:00Z' } as never]);
	models.set([row(previous), row(target)]);
	threadCatalogs.set(new Map(['worker', 'chat'].map((id) => [id, [row(previous), row(target)]])));
	globalThis.fetch = vi.fn(() => new Promise<Response>((resolve, reject) => {
		resolvePatch = resolve; rejectPatch = reject;
	})) as typeof fetch;
});

afterEach(async () => {
	resolvePatch?.(response({ model: target, model_updated_at: '2026-11-06T19:00:01Z' }));
	await Promise.resolve();
	await Promise.resolve();
	if (component) await unmount(component);
	component = undefined;
	host.remove();
	_resetThreadModelRequestsForTests();
	subagentMeta.set(new Map());
	subagentThreadRows.set(new Map());
	globalThis.fetch = originalFetch;
});

it.each(['regular', 'omitted-agent', 'worker'])('paints progress on a %s and keeps the selected check unchanged', (surface) => {
	let id = 'chat';
	if (surface === 'omitted-agent') threads.set([]);
	if (surface === 'worker') {
		id = 'worker';
		acceptThreadModelTruth(id, previous, '2026-11-06T19:00:00Z');
		subagentMeta.set(new Map([[id, { childThreadId: id, parentThreadId: 'chat', name: 'Worker', provider: 'cc', status: 'running' }]]));
		subagentThreadRows.set(new Map([[id, { reasoning_effort: 'high' }]]));
	}
	mountSheet(id);
	const button = clickNext();
	expect(button.querySelector('[aria-label="Switching model"]')).not.toBeNull();
	expect(button.classList.contains('active')).toBe(false);
	expect(host.querySelector('button.active')?.textContent).toContain(previous);
	expect(get(composerSelectionFor)(id).modelId).toBe(previous);
	expect(get(activeModelId)).toBe(previous);
});

it('hands HTTP 202 progress to canonical WebSocket state without an idle interval', async () => {
	mountSheet();
	clickNext();
	const pending: boolean[] = [];
	const unsubscribe = composerSelectionFor.subscribe((select) => pending.push(Boolean(select('chat').switching?.pending_model)));
	resolvePatch(response({ operation_id: 'op', status: 'waiting_for_turn', pending_model: target, checkpoint_required: false }, 202));
	await vi.waitFor(() => expect(get(threadModelRequests).size).toBe(0));
	flushSync();
	expect(pending.every(Boolean)).toBe(true);
	expect(host.querySelector('[aria-label="Switching model"]')).not.toBeNull();
	expect(get(activeModelId)).toBe(previous);
	handleMessage({ type: 'session_updated', thread_id: 'chat', model: target,
		updated_at: '2026-11-06T19:00:02Z', pending_model: null, switch_status: null,
		switch_operation_id: null, switch_error: null }, () => {});
	flushSync();
	expect(get(activeModelId)).toBe(target);
	expect(get(composerSelectionFor)('chat').switching?.pending_model).toBeNull();
	unsubscribe();
});

it('keeps feedback until the matching HTTP request settles after an early WebSocket commit', async () => {
	mountSheet();
	clickNext();
	handleMessage({ type: 'session_updated', thread_id: 'chat', model: target,
		updated_at: '2026-11-06T19:00:02Z' }, () => {});
	flushSync();
	expect(get(activeModelId)).toBe(target);
	expect(get(composerSelectionFor)('chat').switching?.pending_model).toBe(target);
	resolvePatch(response({ model: target, model_updated_at: '2026-11-06T19:00:01Z' }));
	await vi.waitFor(() => expect(get(threadModelRequests).size).toBe(0));
	expect(get(activeModelId)).toBe(target);
});

it('keeps a request through sheet removal and navigation, then settles its own thread', async () => {
	mountSheet();
	clickNext();
	await unmount(component!);
	component = undefined;
	activeThreadId.set('other');
	activeModelId.set('other-model');
	expect(get(threadModelRequests).has('chat')).toBe(true);
	mountSheet('chat');
	clickNext();
	expect(globalThis.fetch).toHaveBeenCalledOnce();
	resolvePatch(response({ model: target, model_updated_at: '2026-11-06T19:00:01Z' }));
	await vi.waitFor(() => expect(get(threadModelRequests).size).toBe(0));
	expect(get(activeModelId)).toBe('other-model');
});

it.each(['rejected', 'thrown', 'unauthenticated'])('settles %s requests with the existing error treatment', async (failure) => {
	if (failure === 'unauthenticated') token.set(null);
	mountSheet();
	clickNext();
	if (failure === 'rejected') resolvePatch(response({ detail: 'Switch refused' }, 409));
	if (failure === 'thrown') rejectPatch(new Error('Transport failed'));
	await vi.waitFor(() => expect(get(threadModelRequests).size).toBe(0));
	expect(get(activeModelId)).toBe(previous);
	expect(get(toasts).at(-1)?.type).toBe('error');
});
